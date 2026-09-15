# Douyin Browser Bridge

VideoGet no longer depends on `douyin-cli`. Douyin discovery/download uses a real Chrome/Edge browser session through Chrome DevTools Protocol (CDP).

## Why

Douyin's web risk-control can reject copied cookies or server-side signed requests even when the same session works in a browser. The browser bridge lets Douyin itself create the requests inside a real browser session, while VideoGet only:

- observes search/detail JSON responses that the browser receives;
- reads playback URLs already resolved by the browser;
- downloads the resolved media stream;
- continues the existing Whisper -> translate -> subtitle/TTS pipeline.

VideoGet does not solve CAPTCHA, synthesize anti-bot tokens, or bypass verification. If Douyin asks for verification, complete it manually in the attached browser and retry.

## Windows + Docker Desktop

Start a dedicated browser profile:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_douyin_browser.ps1
```

Or Edge:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_douyin_browser.ps1 -Browser Edge
```

Then log in to Douyin in that dedicated browser window.

Check CDP on Windows:

```powershell
curl.exe http://localhost:9222/json/version
```

Check that the Docker container can reach it:

```powershell
docker compose exec videoget curl http://host.docker.internal:9222/json/version
```

Check the VideoGet bridge itself:

```powershell
docker compose exec videoget python /app/scripts/douyin_browser_bridge.py health
```

## Environment

Default configuration:

```env
DOUYIN_MODE=browser
DOUYIN_CDP_URL=http://host.docker.internal:9222
DOUYIN_BROWSER_BRIDGE=/app/scripts/douyin_browser_bridge.py
DOUYIN_BROWSER_CONNECT_TIMEOUT_SEC=8
DOUYIN_BROWSER_SEARCH_TIMEOUT_SEC=75
DOUYIN_BROWSER_TIMEOUT_SEC=45
DOUYIN_BROWSER_SCROLLS=6
DOUYIN_BROWSER_DOWNLOAD_TIMEOUT_SEC=120
```

Compatibility modes:

- `browser` — default; requires the CDP browser.
- `hybrid` — browser first, then the existing public search-index fallback for discovery only.
- `public` — public search-index discovery only. Downloads still require the browser bridge.
- old `auto` / `authenticated` values are treated as `browser`;
- old `guest` is treated as `public`.

`DOUYIN_COOKIE`, `DOUYIN_UIFID`, persisted `douyin-cli` auth, and `douyin auth cookie-login` are no longer part of the Douyin path.

## Security

The CDP debug port can control the dedicated browser. Use a dedicated VideoGet browser profile, keep the port on your own/private machine, and never expose port `9222` to the public Internet.
