#!/usr/bin/env python3
"""Resolve fresh Douyin video media URLs through Chromium CDP.

The page itself generates all current anti-bot/signature state. We inject the
optional authenticated cookie, observe real network traffic, ask the rendered
<video> element for currentSrc/src, and return only HTTP(S) media candidates.
Chromium runs in its own process group so renderer/network children cannot keep
temporary profiles alive after the helper exits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def browser_binary(configured: str) -> str:
    configured = configured.strip()
    if configured:
        resolved = shutil.which(configured) or configured
        if Path(resolved).is_file():
            return resolved
        raise RuntimeError(f"browser binary not found: {configured}")
    for candidate in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError("no Chrome/Chromium binary found")


def parse_cookie_header(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name:
            out.append((name, value.strip()))
    return out


def looks_like_media(url: str, mime_type: str = "") -> bool:
    lowered = url.lower()
    mime = mime_type.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return False
    return (
        "douyinvod.com" in lowered
        or "mime_type=video" in lowered
        or "/aweme/v1/play/" in lowered
        or "/aweme/v1/play/?" in lowered
        or lowered.endswith(".mp4")
        or ".mp4?" in lowered
        or mime.startswith("video/")
    )


def normalize_media_url(url: str) -> str:
    url = url.strip().replace("&amp;", "&")
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    return url


class CDP:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse, command_timeout: float = 12.0) -> None:
        self.ws = ws
        self.command_timeout = command_timeout
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.reader_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                message_id = payload.get("id")
                if isinstance(message_id, int):
                    future = self.pending.pop(message_id, None)
                    if future is not None and not future.done():
                        future.set_result(payload)
                else:
                    await self.events.put(payload)
        finally:
            error = RuntimeError("CDP websocket closed")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()

    async def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        command_id = self.next_id
        self.next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[command_id] = future
        await self.ws.send_json({"id": command_id, "method": method, "params": params or {}})
        payload = await asyncio.wait_for(future, timeout=self.command_timeout)
        if "error" in payload:
            raise RuntimeError(f"CDP {method}: {payload['error']}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        if not self.reader_task.done():
            self.reader_task.cancel()
        try:
            await self.reader_task
        except (asyncio.CancelledError, RuntimeError):
            pass


async def wait_for_target(session: aiohttp.ClientSession, port: int, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            async with session.get(
                f"http://127.0.0.1:{port}/json/list",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as response:
                targets = await response.json(content_type=None)
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Chromium CDP target did not appear: {last_error or 'timeout'}")


def stop_browser_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def cleanup_temp_profile(path: str | None) -> None:
    if not path:
        return
    for _ in range(4):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.15)


def append_candidate(candidates: list[str], seen: set[str], value: str, mime_type: str = "") -> None:
    value = normalize_media_url(value)
    if not looks_like_media(value, mime_type):
        return
    if value in seen:
        return
    seen.add(value)
    # Prefer direct CDN URLs over redirect/play API URLs.
    if "douyinvod.com" in value.lower():
        candidates.insert(0, value)
    else:
        candidates.append(value)


async def capture(args: argparse.Namespace) -> dict[str, Any]:
    browser = browser_binary(args.browser_bin)
    port = free_port()
    configured_profile = os.getenv("DOUYIN_BROWSER_PROFILE_DIR", "").strip()
    temp_profile_dir: str | None = None
    if configured_profile:
        profile_dir = configured_profile
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
    else:
        temp_profile_dir = tempfile.mkdtemp(prefix="videoget-douyin-video-")
        profile_dir = temp_profile_dir

    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
        "--hide-scrollbars",
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=1280,900",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
    ]
    if args.no_sandbox:
        command.append("--no-sandbox")
    extra = os.getenv("DOUYIN_BROWSER_EXTRA_ARGS", "").strip()
    if extra:
        command.extend(extra.split())
    command.append("about:blank")

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with aiohttp.ClientSession() as session:
            target = await wait_for_target(session, port, min(args.timeout, 10.0))
            async with session.ws_connect(
                target["webSocketDebuggerUrl"],
                origin="http://127.0.0.1",
                timeout=10.0,
                max_msg_size=32 * 1024 * 1024,
            ) as ws:
                cdp = CDP(ws)
                try:
                    await cdp.command("Network.enable")
                    await cdp.command("Network.setCacheDisabled", {"cacheDisabled": True})
                    await cdp.command("Page.enable")
                    await cdp.command("Runtime.enable")

                    user_agent = os.getenv("DOUYIN_USER_AGENT", "").strip()
                    if user_agent:
                        await cdp.command("Network.setUserAgentOverride", {"userAgent": user_agent})

                    cookie_pairs = parse_cookie_header(os.getenv("DOUYIN_COOKIE", ""))
                    for name, value in cookie_pairs:
                        try:
                            await cdp.command(
                                "Network.setCookie",
                                {
                                    "name": name,
                                    "value": value,
                                    "domain": ".douyin.com",
                                    "path": "/",
                                    "secure": True,
                                },
                            )
                        except Exception:
                            pass

                    candidates: list[str] = []
                    seen: set[str] = set()
                    await cdp.command("Page.navigate", {"url": args.url})
                    deadline = time.monotonic() + args.render_seconds
                    next_probe = time.monotonic() + 1.0
                    title = ""
                    final_url = args.url

                    while time.monotonic() < deadline and len(candidates) < args.max_candidates:
                        timeout = min(0.35, max(0.05, deadline - time.monotonic()))
                        try:
                            event = await asyncio.wait_for(cdp.events.get(), timeout=timeout)
                        except asyncio.TimeoutError:
                            event = None

                        if event:
                            method = event.get("method")
                            params = event.get("params") or {}
                            if method == "Network.requestWillBeSent":
                                request = params.get("request") or {}
                                append_candidate(candidates, seen, str(request.get("url") or ""))
                            elif method == "Network.responseReceived":
                                response = params.get("response") or {}
                                append_candidate(
                                    candidates,
                                    seen,
                                    str(response.get("url") or ""),
                                    str(response.get("mimeType") or ""),
                                )

                        now = time.monotonic()
                        if now >= next_probe:
                            next_probe = now + 1.0
                            expression = """
(() => {
  const urls = [];
  const v = document.querySelector('video');
  if (v) {
    try { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (_) {}
    if (v.currentSrc) urls.push(v.currentSrc);
    if (v.src) urls.push(v.src);
    for (const s of v.querySelectorAll('source')) if (s.src) urls.push(s.src);
  }
  for (const e of performance.getEntriesByType('resource')) if (e && e.name) urls.push(e.name);
  return {urls, url: location.href, title: document.title};
})()
"""
                            try:
                                result = await cdp.command(
                                    "Runtime.evaluate",
                                    {"expression": expression, "returnByValue": True},
                                )
                                value = (((result.get("result") or {}).get("value")) or {})
                                if isinstance(value, dict):
                                    for item in value.get("urls") or []:
                                        append_candidate(candidates, seen, str(item))
                                    final_url = str(value.get("url") or final_url)
                                    title = str(value.get("title") or title)
                            except Exception:
                                pass

                    # One final probe catches a source attached near the deadline.
                    try:
                        result = await cdp.command(
                            "Runtime.evaluate",
                            {
                                "expression": "({currentSrc:document.querySelector('video')?.currentSrc||'',src:document.querySelector('video')?.src||'',url:location.href,title:document.title})",
                                "returnByValue": True,
                            },
                        )
                        value = (((result.get("result") or {}).get("value")) or {})
                        if isinstance(value, dict):
                            append_candidate(candidates, seen, str(value.get("currentSrc") or ""))
                            append_candidate(candidates, seen, str(value.get("src") or ""))
                            final_url = str(value.get("url") or final_url)
                            title = str(value.get("title") or title)
                    except Exception:
                        pass

                    return {
                        "candidates": candidates[: args.max_candidates],
                        "finalUrl": final_url,
                        "title": title,
                        "cookieCount": len(cookie_pairs),
                    }
                finally:
                    await cdp.close()
    finally:
        stop_browser_tree(process)
        cleanup_temp_profile(temp_profile_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--browser-bin", default=os.getenv("DOUYIN_BROWSER_BIN", "chromium"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("DOUYIN_BROWSER_CDP_TIMEOUT_SEC", "25")))
    parser.add_argument(
        "--render-seconds",
        type=float,
        default=max(4.0, float(os.getenv("DOUYIN_BROWSER_CDP_RENDER_MS", "10000")) / 1000.0),
    )
    parser.add_argument("--max-candidates", type=int, default=int(os.getenv("DOUYIN_BROWSER_CDP_CANDIDATES", "12")))
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        default=os.getenv("DOUYIN_BROWSER_NO_SANDBOX", "false").strip().lower() in {"1", "true", "yes", "on"},
    )
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    try:
        payload = await asyncio.wait_for(capture(args), timeout=args.timeout)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
