#!/usr/bin/env python3
"""Douyin native search through Chromium CDP with a persistent browser session.

Douyin changes its web search endpoints and short-lived browser tokens often.
This helper therefore lets Douyin's own page create signed requests, keeps a
persistent Chromium profile when VideoGet has a download directory, captures
multiple search-response shapes, and also harvests rendered /video/ links as a
fallback. Authentication is treated as browser state, not as a Cookie header.
Cookie/storage values are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import aiohttp

KNOWN_SEARCH_MARKERS = (
    "/aweme/v1/web/general/search/",
    "/aweme/v1/web/search/",
    "/aweme/v1/web/search/item/",
    "/aweme/v1/web/search/single/",
)


def is_search_response_url(response_url: str, mime_type: str = "") -> bool:
    """Return True for Douyin JSON/XHR responses that can contain search items."""
    lowered = response_url.lower()
    if "douyin.com" not in lowered:
        return False
    if any(marker in lowered for marker in KNOWN_SEARCH_MARKERS):
        return True
    # Endpoint names drift. Keep this deliberately broad but limited to Douyin
    # aweme/search traffic so unrelated page resources are not buffered.
    if "/aweme/" in lowered and "search" in lowered:
        return True
    if "/search/" in lowered and ("application/json" in mime_type.lower() or "json" in mime_type.lower()):
        return True
    return False


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


def rewrite_remote_ws_url(ws_url: str, cdp_base: str) -> str:
    """Chrome often reports ws://127.0.0.1 even when reached through host.docker.internal."""
    ws = urlparse(ws_url)
    base = urlparse(cdp_base if "://" in cdp_base else "http://" + cdp_base)
    scheme = "wss" if base.scheme == "https" else "ws"
    return urlunparse((scheme, base.netloc, ws.path, ws.params, ws.query, ws.fragment))


async def open_remote_target(
    session: aiohttp.ClientSession,
    cdp_base: str,
    page_url: str,
    timeout: float,
) -> dict[str, Any]:
    base = cdp_base.rstrip("/")
    encoded = quote(page_url, safe=":/?=&%")
    last_error: Exception | None = None

    # Chrome DevTools supports PUT /json/new?<url>. Creating a dedicated tab avoids
    # hijacking whatever the user is currently viewing in the authenticated browser.
    try:
        async with session.put(
            f"{base}/json/new?{encoded}",
            timeout=aiohttp.ClientTimeout(total=min(timeout, 5.0)),
        ) as response:
            if 200 <= response.status < 300:
                target = await response.json(content_type=None)
                if target.get("webSocketDebuggerUrl"):
                    target["webSocketDebuggerUrl"] = rewrite_remote_ws_url(
                        str(target["webSocketDebuggerUrl"]), base
                    )
                    target["_videogetCreated"] = True
                    return target
    except Exception as exc:
        last_error = exc

    try:
        async with session.get(
            f"{base}/json/list",
            timeout=aiohttp.ClientTimeout(total=min(timeout, 5.0)),
        ) as response:
            targets = await response.json(content_type=None)
            pages = [
                item
                for item in targets
                if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
            ]
            preferred = next(
                (item for item in pages if "douyin.com" in str(item.get("url") or "")),
                pages[0] if pages else None,
            )
            if preferred:
                preferred["webSocketDebuggerUrl"] = rewrite_remote_ws_url(
                    str(preferred["webSocketDebuggerUrl"]), base
                )
                preferred["_videogetCreated"] = False
                return preferred
    except Exception as exc:
        last_error = exc

    raise RuntimeError(f"remote Chromium CDP target unavailable: {last_error or 'no page target'}")


async def close_remote_target(
    session: aiohttp.ClientSession,
    cdp_base: str,
    target_id: str,
) -> None:
    if not target_id:
        return
    try:
        async with session.get(
            f"{cdp_base.rstrip('/')}/json/close/{target_id}",
            timeout=aiohttp.ClientTimeout(total=2),
        ):
            pass
    except Exception:
        pass


def stop_browser_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate Chromium's whole process group; cleanup errors must never mask search output."""
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
    # Chrome children can briefly recreate files while shutting down. Best-effort
    # deletion is enough in /tmp and must not turn a successful search into Errno 39.
    for _ in range(4):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.15)


async def capture_target(
    session: aiohttp.ClientSession,
    target: dict[str, Any],
    args: argparse.Namespace,
    page_url: str,
    session_source: str,
    inject_legacy_cookie: bool,
) -> dict[str, Any]:
    ws_url = str(target.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        raise RuntimeError("CDP page target has no websocket URL")

    async with session.ws_connect(
        ws_url,
        origin="http://127.0.0.1",
        timeout=10.0,
        max_msg_size=64 * 1024 * 1024,
    ) as ws:
        cdp = CDP(ws)
        try:
            await cdp.command("Network.enable", {"maxTotalBufferSize": 64 * 1024 * 1024})
            await cdp.command("Page.enable")
            await cdp.command("Runtime.enable")

            # Only override UA for the Chromium instance VideoGet launched itself.
            # A connected real browser must keep its own UA/fingerprint coherent.
            if session_source != "remote-cdp":
                user_agent = os.getenv("DOUYIN_USER_AGENT", "").strip()
                if user_agent:
                    await cdp.command("Network.setUserAgentOverride", {"userAgent": user_agent})

            # Legacy bootstrap only. Douyin's current browser search can use state
            # from browser storage and generated headers without sending Cookie on
            # the search XHR itself, so VideoGet never treats this as auth proof.
            cookie_pairs: list[tuple[str, str]] = []
            if inject_legacy_cookie:
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
                    except Exception as exc:
                        print(f"warning: failed to inject legacy cookie {name}: {exc}", file=sys.stderr)

            await cdp.command("Page.navigate", {"url": page_url})

            api_bodies: list[str] = []
            target_requests: set[str] = set()
            completed_requests: set[str] = set()
            captured_urls: list[str] = []
            deadline = time.monotonic() + args.render_seconds
            next_scroll = time.monotonic() + 2.5
            scrolls = 0
            final_url = page_url
            title = ""

            while time.monotonic() < deadline and len(api_bodies) < args.max_pages:
                timeout = min(0.4, max(0.05, deadline - time.monotonic()))
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
                        mime_type = str(response.get("mimeType") or "")
                        request_id = str(params.get("requestId") or "")
                        if request_id and is_search_response_url(response_url, mime_type):
                            target_requests.add(request_id)
                            if response_url not in captured_urls:
                                captured_urls.append(response_url)
                    elif method == "Network.loadingFinished":
                        request_id = str(params.get("requestId") or "")
                        if request_id in target_requests and request_id not in completed_requests:
                            completed_requests.add(request_id)
                            try:
                                body_result = await cdp.command(
                                    "Network.getResponseBody", {"requestId": request_id}
                                )
                                body = str(body_result.get("body") or "")
                                if body_result.get("base64Encoded"):
                                    body = base64.b64decode(body).decode(
                                        "utf-8", errors="replace"
                                    )
                                if body:
                                    api_bodies.append(body)
                            except Exception as exc:
                                print(
                                    f"warning: could not read Douyin search response: {exc}",
                                    file=sys.stderr,
                                )
                    elif method == "Network.loadingFailed":
                        target_requests.discard(str(params.get("requestId") or ""))

                now = time.monotonic()
                if now >= next_scroll and scrolls < args.scrolls:
                    scrolls += 1
                    next_scroll = now + 2.0
                    try:
                        await cdp.command(
                            "Runtime.evaluate",
                            {
                                "expression": (
                                    "window.scrollTo(0, Math.max(document.body.scrollHeight,"
                                    " document.documentElement.scrollHeight)); true"
                                ),
                                "returnByValue": True,
                            },
                        )
                    except Exception:
                        pass

            dom = ""
            video_links: list[str] = []
            local_storage_count = 0
            session_storage_count = 0
            has_indexed_db = False
            try:
                result = await cdp.command(
                    "Runtime.evaluate",
                    {
                        "expression": """({
                            html: document.documentElement.outerHTML,
                            url: location.href,
                            title: document.title,
                            videoLinks: Array.from(document.querySelectorAll('a[href*="/video/"]'))
                                .map(a => a.href)
                                .filter(Boolean),
                            localStorageCount: localStorage.length,
                            sessionStorageCount: sessionStorage.length,
                            hasIndexedDB: !!window.indexedDB
                        })""",
                        "returnByValue": True,
                    },
                )
                value = (((result.get("result") or {}).get("value")) or {})
                dom = str(value.get("html") or "")
                final_url = str(value.get("url") or final_url)
                title = str(value.get("title") or "")
                video_links = [str(item) for item in (value.get("videoLinks") or []) if item]
                local_storage_count = int(value.get("localStorageCount") or 0)
                session_storage_count = int(value.get("sessionStorageCount") or 0)
                has_indexed_db = bool(value.get("hasIndexedDB"))
            except Exception:
                pass

            if len(dom.encode("utf-8", errors="ignore")) > args.dom_max_bytes:
                dom = ""

            browser_cookie_count = 0
            cookie_names: list[str] = []
            try:
                cookie_result = await cdp.command("Network.getAllCookies")
                browser_cookies = cookie_result.get("cookies") or []
                browser_cookie_count = len(browser_cookies)
                cookie_names = sorted(
                    {
                        str(item.get("name") or "")
                        for item in browser_cookies
                        if item.get("name")
                    }
                )
            except Exception:
                pass

            return {
                "apiBodies": api_bodies,
                "videoLinks": video_links,
                "dom": dom,
                "finalUrl": final_url,
                "title": title,
                "cookieCount": browser_cookie_count,
                "cookieNames": cookie_names,
                "capturedSearchUrls": captured_urls[:20],
                "profilePersistent": session_source != "temporary-profile",
                "sessionSource": session_source,
                "localStorageCount": local_storage_count,
                "sessionStorageCount": session_storage_count,
                "hasIndexedDB": has_indexed_db,
            }
        finally:
            await cdp.close()


async def capture(args: argparse.Namespace) -> dict[str, Any]:
    keyword_path = quote(args.keyword, safe="")
    page_url = f"https://www.douyin.com/search/{keyword_path}?type=general"

    remote_cdp = os.getenv("DOUYIN_CDP_URL", "").strip()
    if remote_cdp:
        async with aiohttp.ClientSession() as session:
            target = await open_remote_target(session, remote_cdp, page_url, args.timeout)
            created_target_id = (
                str(target.get("id") or "") if target.get("_videogetCreated") else ""
            )
            try:
                return await capture_target(
                    session,
                    target,
                    args,
                    page_url,
                    session_source="remote-cdp",
                    inject_legacy_cookie=False,
                )
            finally:
                await close_remote_target(session, remote_cdp, created_target_id)

    browser = browser_binary(args.browser_bin)
    port = free_port()
    configured_profile = os.getenv("DOUYIN_NATIVE_SEARCH_PROFILE_DIR", "").strip() or os.getenv(
        "DOUYIN_BROWSER_PROFILE_DIR", ""
    ).strip()
    download_dir = os.getenv("DOWNLOAD_DIR", "").strip()
    if not configured_profile and download_dir:
        configured_profile = str(Path(download_dir) / ".douyin-profile")

    temp_profile_dir: str | None = None
    if configured_profile:
        profile_dir = configured_profile
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        session_source = "persistent-profile"
    else:
        temp_profile_dir = tempfile.mkdtemp(prefix="videoget-douyin-cdp-")
        profile_dir = temp_profile_dir
        session_source = "temporary-profile"

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

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with aiohttp.ClientSession() as session:
            target = await wait_for_target(session, port, min(args.timeout, 10.0))
            return await capture_target(
                session,
                target,
                args,
                page_url,
                session_source=session_source,
                inject_legacy_cookie=True,
            )
    finally:
        stop_browser_tree(process)
        cleanup_temp_profile(temp_profile_dir)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--browser-bin", default=os.getenv("DOUYIN_BROWSER_BIN", "chromium"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC", "25")))
    parser.add_argument(
        "--render-seconds",
        type=float,
        default=max(4.0, float(os.getenv("DOUYIN_NATIVE_SEARCH_RENDER_MS", "8000")) / 1000.0),
    )
    parser.add_argument("--scrolls", type=int, default=int(os.getenv("DOUYIN_NATIVE_SEARCH_SCROLLS", "2")))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("DOUYIN_NATIVE_SEARCH_MAX_PAGES", "3")))
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
