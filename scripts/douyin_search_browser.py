#!/usr/bin/env python3
"""Resolve Douyin keyword search through Chromium's own signed web requests.

The helper launches a local Chromium instance with CDP enabled, injects the
optional DOUYIN_COOKIE into .douyin.com, opens the native search page, and
captures responses from /aweme/v1/web/general/search/single/. Only captured
JSON plus a bounded DOM snapshot are written to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

SEARCH_ENDPOINT = "/aweme/v1/web/general/search/single/"


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
    cookies: list[tuple[str, str]] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name:
            cookies.append((name, value.strip()))
    return cookies


class CDP:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse, command_timeout: float = 15.0) -> None:
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
    url = f"http://127.0.0.1:{port}/json/list"
    while time.monotonic() < deadline:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as response:
                targets = await response.json(content_type=None)
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Chromium CDP target did not appear: {last_error or 'timeout'}")


async def capture(args: argparse.Namespace) -> dict[str, Any]:
    browser = browser_binary(args.browser_bin)
    port = free_port()

    configured_profile = os.getenv("DOUYIN_NATIVE_SEARCH_PROFILE_DIR", "").strip() or os.getenv(
        "DOUYIN_BROWSER_PROFILE_DIR", ""
    ).strip()
    temp_profile: tempfile.TemporaryDirectory[str] | None = None
    if configured_profile:
        profile_dir = configured_profile
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
    else:
        temp_profile = tempfile.TemporaryDirectory(prefix="videoget-douyin-cdp-")
        profile_dir = temp_profile.name

    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
        "--hide-scrollbars",
        "--window-size=1440,1600",
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

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        async with aiohttp.ClientSession() as session:
            target = await wait_for_target(session, port, min(args.timeout, 12.0))
            async with session.ws_connect(
                target["webSocketDebuggerUrl"],
                origin="http://127.0.0.1",
                timeout=10.0,
                max_msg_size=64 * 1024 * 1024,
            ) as ws:
                cdp = CDP(ws)
                try:
                    await cdp.command("Network.enable", {"maxTotalBufferSize": 64 * 1024 * 1024})
                    await cdp.command("Page.enable")
                    await cdp.command("Runtime.enable")

                    user_agent = os.getenv("DOUYIN_USER_AGENT", "").strip()
                    if user_agent:
                        await cdp.command("Network.setUserAgentOverride", {"userAgent": user_agent})

                    cookie_pairs = parse_cookie_header(os.getenv("DOUYIN_COOKIE", ""))
                    for name, value in cookie_pairs:
                        result = await cdp.command(
                            "Network.setCookie",
                            {
                                "name": name,
                                "value": value,
                                "domain": ".douyin.com",
                                "path": "/",
                                "secure": True,
                            },
                        )
                        if result.get("success") is False:
                            print(f"warning: failed to inject cookie {name}", file=sys.stderr)

                    keyword_path = quote(args.keyword, safe="")
                    page_url = f"https://www.douyin.com/search/{keyword_path}?type=general"
                    await cdp.command("Page.navigate", {"url": page_url})

                    api_bodies: list[str] = []
                    target_requests: set[str] = set()
                    completed_requests: set[str] = set()
                    deadline = time.monotonic() + args.render_seconds
                    next_scroll = time.monotonic() + 3.0
                    scrolls = 0
                    final_url = page_url
                    title = ""

                    while time.monotonic() < deadline and len(api_bodies) < args.max_pages:
                        timeout = min(0.5, max(0.05, deadline - time.monotonic()))
                        try:
                            event = await asyncio.wait_for(cdp.events.get(), timeout=timeout)
                        except asyncio.TimeoutError:
                            event = None

                        if event:
                            method = event.get("method")
                            params = event.get("params") or {}
                            if method == "Network.responseReceived":
                                response = params.get("response") or {}
                                response_url = str(response.get("url") or "")
                                request_id = str(params.get("requestId") or "")
                                if SEARCH_ENDPOINT in response_url and request_id:
                                    target_requests.add(request_id)
                            elif method == "Network.loadingFinished":
                                request_id = str(params.get("requestId") or "")
                                if request_id in target_requests and request_id not in completed_requests:
                                    completed_requests.add(request_id)
                                    try:
                                        body_result = await cdp.command("Network.getResponseBody", {"requestId": request_id})
                                        body = str(body_result.get("body") or "")
                                        if body_result.get("base64Encoded"):
                                            body = base64.b64decode(body).decode("utf-8", errors="replace")
                                        if body:
                                            api_bodies.append(body)
                                    except Exception as exc:
                                        print(f"warning: could not read Douyin search response: {exc}", file=sys.stderr)
                            elif method == "Network.loadingFailed":
                                request_id = str(params.get("requestId") or "")
                                target_requests.discard(request_id)

                        now = time.monotonic()
                        if now >= next_scroll and scrolls < args.scrolls:
                            scrolls += 1
                            next_scroll = now + 2.5
                            try:
                                await cdp.command(
                                    "Runtime.evaluate",
                                    {
                                        "expression": "window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)); true",
                                        "returnByValue": True,
                                    },
                                )
                            except Exception:
                                pass

                    try:
                        result = await cdp.command(
                            "Runtime.evaluate",
                            {
                                "expression": "({html:document.documentElement.outerHTML,url:location.href,title:document.title})",
                                "returnByValue": True,
                            },
                        )
                        value = (((result.get("result") or {}).get("value")) or {})
                        dom = str(value.get("html") or "")
                        final_url = str(value.get("url") or final_url)
                        title = str(value.get("title") or "")
                    except Exception:
                        dom = ""

                    if len(dom.encode("utf-8", errors="ignore")) > args.dom_max_bytes:
                        dom = ""

                    return {
                        "apiBodies": api_bodies,
                        "dom": dom,
                        "finalUrl": final_url,
                        "title": title,
                        "cookieCount": len(cookie_pairs),
                    }
                finally:
                    await cdp.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        if temp_profile is not None:
            temp_profile.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--browser-bin", default=os.getenv("DOUYIN_BROWSER_BIN", "chromium"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC", "45")))
    parser.add_argument(
        "--render-seconds",
        type=float,
        default=max(4.0, float(os.getenv("DOUYIN_NATIVE_SEARCH_RENDER_MS", "12000")) / 1000.0),
    )
    parser.add_argument("--scrolls", type=int, default=int(os.getenv("DOUYIN_NATIVE_SEARCH_SCROLLS", "4")))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("DOUYIN_NATIVE_SEARCH_MAX_PAGES", "5")))
    parser.add_argument(
        "--dom-max-bytes",
        type=int,
        default=int(os.getenv("DOUYIN_NATIVE_SEARCH_DOM_MAX_MB", "32")) * 1024 * 1024,
    )
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
