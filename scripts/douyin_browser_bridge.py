#!/usr/bin/env python3
"""Douyin browser/CDP bridge for VideoGet.

This module attaches to a real Chromium-based browser that the user started with
remote debugging enabled. Douyin itself creates the authenticated/risk-control
requests; VideoGet only observes browser responses and downloads media URLs that
the browser has already resolved.

The bridge never solves captchas, generates anti-bot signatures, or bypasses
verification. If Douyin presents a verification page, the user must complete it
manually in the attached browser and retry.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    import fcntl  # Linux container; optional when run directly on Windows.
except ImportError:  # pragma: no cover - Windows helper/testing only.
    fcntl = None

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright

DEFAULT_CDP_URL = "http://host.docker.internal:9222"
VIDEO_ID_RE = re.compile(r"/video/([0-9]{8,})", re.I)
VERIFY_MARKERS = ("验证码", "安全验证", "完成验证", "访问过于频繁", "verify")
MEDIA_HOST_HINTS = ("douyinvod.com", "bytevcdn", "douyin.com/aweme/v1/play")
MIN_MEDIA_BYTES = 64 * 1024


def env_int(name: str, fallback: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip())
        return value if value > 0 else fallback
    except ValueError:
        return fallback


def cdp_url() -> str:
    return (os.getenv("DOUYIN_CDP_URL") or DEFAULT_CDP_URL).strip().rstrip("/")


def first_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    urls = value.get("url_list") or value.get("urlList") or []
    if isinstance(urls, list):
        for item in urls:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
    for key in ("url", "uri"):
        item = value.get(key)
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            return item
    return ""


def normalize_aweme(aweme: dict[str, Any], keyword: str = "") -> dict[str, Any] | None:
    aweme_id = str(aweme.get("aweme_id") or aweme.get("id") or "").strip()
    if not aweme_id.isdigit():
        return None

    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    stats = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
    video = aweme.get("video") if isinstance(aweme.get("video"), dict) else {}

    cover = (
        first_url(video.get("cover"))
        or first_url(video.get("origin_cover"))
        or first_url(video.get("dynamic_cover"))
    )
    play_url = (
        first_url(video.get("play_addr"))
        or first_url(video.get("play_addr_h264"))
        or first_url(video.get("download_addr"))
        or first_url(aweme.get("download_addr"))
    )
    duration = aweme.get("duration") or video.get("duration") or 0

    def number(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "id": aweme_id,
        "desc": str(aweme.get("desc") or "").strip(),
        "author_nickname": str(author.get("nickname") or "").strip(),
        "cover": cover,
        "duration": number(duration),
        "play_count": number(stats.get("play_count")),
        "digg_count": number(stats.get("digg_count")),
        "comment_count": number(stats.get("comment_count")),
        "share_count": number(stats.get("share_count")),
        "download_addr": play_url,
        "time": number(aweme.get("create_time")),
        "search_source": keyword,
    }


def collect_awemes(payload: Any, out: dict[str, dict[str, Any]], keyword: str = "") -> None:
    stack: list[Any] = [payload]
    visited = 0
    while stack and visited < 50000:
        visited += 1
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, dict):
            continue

        nested = value.get("aweme_info")
        if isinstance(nested, dict):
            item = normalize_aweme(nested, keyword)
            if item:
                out[item["id"]] = item

        if "aweme_id" in value and ("video" in value or "statistics" in value):
            item = normalize_aweme(value, keyword)
            if item:
                out[item["id"]] = item

        stack.extend(value.values())


def response_json(response: Response) -> Any | None:
    try:
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            return None
        return response.json()
    except Exception:
        return None


def verification_required(page: Page) -> bool:
    url = (page.url or "").lower()
    if any(marker in url for marker in ("captcha", "verify", "challenge")):
        return True
    try:
        body = page.locator("body").inner_text(timeout=1500)[:12000]
    except Exception:
        return False
    lowered = body.lower()
    return any(marker.lower() in lowered for marker in VERIFY_MARKERS)


def browser_context(playwright):
    endpoint = cdp_url()
    browser = playwright.chromium.connect_over_cdp(
        endpoint,
        timeout=env_int("DOUYIN_BROWSER_CONNECT_TIMEOUT_SEC", 8) * 1000,
    )
    if not browser.contexts:
        raise RuntimeError(
            "CDP browser has no usable context. Start Chrome/Edge with a dedicated "
            "--user-data-dir and remote debugging, then open Douyin."
        )
    return browser.contexts[0]


@contextmanager
def operation_lock():
    path = os.getenv("DOUYIN_BROWSER_LOCK_FILE", "/tmp/videoget-douyin-browser.lock")
    handle = open(path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def search(keyword: str, limit: int) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    timeout_ms = env_int("DOUYIN_BROWSER_TIMEOUT_SEC", 45) * 1000
    scrolls = env_int("DOUYIN_BROWSER_SCROLLS", 6)

    with operation_lock(), sync_playwright() as playwright:
        context = browser_context(playwright)
        page = context.new_page()
        try:
            def on_response(response: Response) -> None:
                url = response.url.lower()
                if "douyin.com" not in url:
                    return
                if not any(token in url for token in ("search", "aweme", "feed")):
                    return
                payload = response_json(response)
                if payload is not None:
                    collect_awemes(payload, items, keyword)

            page.on("response", on_response)
            target = f"https://www.douyin.com/search/{quote(keyword, safe='')}?type=video"
            page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1800)

            if verification_required(page):
                raise RuntimeError(
                    "Douyin requires browser verification. Complete the verification "
                    "manually in the attached Chrome/Edge window, then retry."
                )

            for _ in range(scrolls):
                if len(items) >= limit:
                    break
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(1100)
                if verification_required(page):
                    raise RuntimeError(
                        "Douyin requested verification while scrolling search results. "
                        "Complete it manually in the attached browser, then retry."
                    )

            try:
                links = page.locator('a[href*="/video/"]')
                count = min(links.count(), max(limit * 3, 30))
                for index in range(count):
                    href = links.nth(index).get_attribute("href") or ""
                    match = VIDEO_ID_RE.search(href)
                    if not match:
                        continue
                    aweme_id = match.group(1)
                    if aweme_id in items:
                        continue
                    try:
                        title = links.nth(index).inner_text(timeout=400).strip()
                    except Exception:
                        title = ""
                    items[aweme_id] = {
                        "id": aweme_id,
                        "desc": title,
                        "author_nickname": "",
                        "cover": "",
                        "duration": 0,
                        "play_count": 0,
                        "digg_count": 0,
                        "comment_count": 0,
                        "share_count": 0,
                        "download_addr": "",
                        "time": 0,
                        "search_source": keyword,
                    }
                    if len(items) >= limit:
                        break
            except Exception:
                pass

            return list(items.values())[:limit]
        finally:
            try:
                page.close()
            except Exception:
                pass


def cookie_header(context: BrowserContext, raw_url: str) -> str:
    host = (urlparse(raw_url).hostname or "").lower()
    pairs: list[str] = []
    try:
        cookies = context.cookies()
    except Exception:
        cookies = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if domain and host and not (host == domain or host.endswith("." + domain)):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def stream_media(context: BrowserContext, page: Page, raw_url: str, media_url: str, destination: Path) -> None:
    user_agent = page.evaluate("navigator.userAgent")
    headers = {
        "User-Agent": str(user_agent or ""),
        "Referer": raw_url,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    cookies = cookie_header(context, media_url)
    if cookies:
        headers["Cookie"] = cookies

    request = Request(media_url, headers=headers, method="GET")
    timeout = env_int("DOUYIN_BROWSER_DOWNLOAD_TIMEOUT_SEC", 120)
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc

    with response:
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise RuntimeError(f"unexpected content-type {content_type or 'unknown'}")
        if "mpegurl" in content_type or ".m3u8" in media_url.lower():
            raise RuntimeError("resolved media is HLS; direct MP4 stream was not available")

        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)

    if not destination.exists() or destination.stat().st_size < MIN_MEDIA_BYTES:
        try:
            destination.unlink()
        except OSError:
            pass
        raise RuntimeError("downloaded response was too small to be a valid video")


def download(raw_url: str, output_dir: str, provided_media_url: str = "") -> dict[str, Any]:
    match = VIDEO_ID_RE.search(raw_url)
    aweme_id = match.group(1) if match else str(int(time.time()))
    timeout_ms = env_int("DOUYIN_BROWSER_TIMEOUT_SEC", 45) * 1000
    network_media: list[str] = []
    awemes: dict[str, dict[str, Any]] = {}

    with operation_lock(), sync_playwright() as playwright:
        context = browser_context(playwright)
        page = context.new_page()
        try:
            def on_response(response: Response) -> None:
                url = response.url
                lowered = url.lower()
                content_type = (response.headers.get("content-type") or "").lower()
                try:
                    resource_type = response.request.resource_type
                except Exception:
                    resource_type = ""

                if "douyin.com" in lowered and "json" in content_type:
                    payload = response_json(response)
                    if payload is not None:
                        collect_awemes(payload, awemes)

                if resource_type == "media" or content_type.startswith("video/") or any(
                    hint in lowered for hint in MEDIA_HOST_HINTS
                ):
                    if url.startswith(("http://", "https://")):
                        network_media.append(url)

            page.on("response", on_response)
            page.goto(raw_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)

            if verification_required(page):
                raise RuntimeError(
                    "Douyin requires browser verification. Complete it manually in the "
                    "attached Chrome/Edge window, then retry this job."
                )

            candidates: list[tuple[str, str]] = []
            if provided_media_url.startswith(("http://", "https://")):
                candidates.append(("search-response", provided_media_url))

            item = awemes.get(aweme_id)
            if item and str(item.get("download_addr") or "").startswith(("http://", "https://")):
                candidates.append(("page-json", str(item["download_addr"])))

            try:
                dom_urls = page.eval_on_selector_all(
                    "video",
                    "els => els.map(v => v.currentSrc || v.src).filter(Boolean)",
                )
                for url in dom_urls or []:
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        candidates.append(("video-element", url))
            except Exception:
                pass

            try:
                perf_urls = page.evaluate(
                    """() => performance.getEntriesByType('resource')
                      .map(x => x.name)
                      .filter(x => /^https?:/.test(x) &&
                        (x.includes('douyinvod.com') ||
                         x.includes('/aweme/v1/play') ||
                         x.includes('/video/tos/')))"""
                )
                for url in perf_urls or []:
                    if isinstance(url, str):
                        candidates.append(("performance", url))
            except Exception:
                pass

            for url in network_media:
                candidates.append(("network-media", url))

            seen: set[str] = set()
            failures: list[str] = []
            destination = Path(output_dir) / f"douyin-browser [{aweme_id}].mp4"
            for strategy, candidate in candidates:
                candidate = candidate.replace("&amp;", "&").strip()
                if not candidate.startswith(("http://", "https://")) or candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    stream_media(context, page, raw_url, candidate, destination)
                    return {
                        "path": str(destination),
                        "strategy": strategy,
                        "mediaUrl": candidate,
                    }
                except Exception as exc:
                    failures.append(f"{strategy}: {exc}")
                    try:
                        destination.unlink()
                    except OSError:
                        pass

            detail = " | ".join(failures[:6]) if failures else "browser exposed no HTTP media URL"
            raise RuntimeError(
                "Douyin page opened in the real browser, but VideoGet could not download "
                f"the resolved media stream ({detail})."
            )
        finally:
            try:
                page.close()
            except Exception:
                pass


def health() -> dict[str, Any]:
    endpoint = cdp_url() + "/json/version"
    try:
        with urlopen(endpoint, timeout=env_int("DOUYIN_BROWSER_CONNECT_TIMEOUT_SEC", 8)) as response:
            payload = json.loads(response.read(512 * 1024).decode("utf-8", "replace"))
    except Exception as exc:
        raise RuntimeError(
            f"cannot reach Douyin browser CDP at {endpoint}: {exc}. "
            "Start scripts/start_douyin_browser.ps1 on Windows and keep that browser open."
        ) from exc
    return {
        "ok": True,
        "cdp": cdp_url(),
        "browser": payload.get("Browser", ""),
        "protocolVersion": payload.get("Protocol-Version", ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VideoGet Douyin Browser/CDP bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    search_parser = sub.add_parser("search")
    search_parser.add_argument("--keyword", required=True)
    search_parser.add_argument("--limit", type=int, default=20)

    download_parser = sub.add_parser("download")
    download_parser.add_argument("--url", required=True)
    download_parser.add_argument("--output-dir", required=True)
    download_parser.add_argument("--media-url", default="")

    sub.add_parser("health")
    return parser


def parse_args_compat(argv: list[str]) -> argparse.Namespace:
    """Accept both the new subcommands and VideoGet's old downloader argv shape.

    The Go download manager historically invokes DOUYIN_BIN as:
      -u <url> -t aweme -p <output-dir>
    Keeping that small argv contract lets us replace douyin-cli without touching
    the stable Bilibili/public download manager.
    """
    if argv and argv[0] in {"search", "download", "health"}:
        return build_parser().parse_args(argv)

    legacy = argparse.ArgumentParser(add_help=False)
    legacy.add_argument("-u", dest="target", required=True)
    legacy.add_argument("-t", dest="kind", required=True)
    legacy.add_argument("-p", dest="output_dir", default="")
    legacy.add_argument("-l", dest="limit", type=int, default=20)
    legacy.add_argument("--no-download", action="store_true")
    args, _ = legacy.parse_known_args(argv)
    kind = str(args.kind or "").strip().lower()
    if kind == "aweme":
        if not args.output_dir:
            legacy.error("-p output directory is required for aweme download")
        return argparse.Namespace(
            command="download",
            url=args.target,
            output_dir=args.output_dir,
            media_url="",
        )
    if kind == "search":
        return argparse.Namespace(
            command="search",
            keyword=args.target,
            limit=max(1, args.limit),
        )
    legacy.error(f"unsupported legacy task type: {args.kind}")
    raise AssertionError("unreachable")


def main() -> int:
    args = parse_args_compat(sys.argv[1:])
    try:
        if args.command == "search":
            json.dump(search(args.keyword, max(1, args.limit)), sys.stdout, ensure_ascii=False)
        elif args.command == "download":
            json.dump(
                download(args.url, args.output_dir, getattr(args, "media_url", "")),
                sys.stdout,
                ensure_ascii=False,
            )
        else:
            json.dump(health(), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        sys.stderr.write(f"VideoGet Douyin Browser Bridge: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
