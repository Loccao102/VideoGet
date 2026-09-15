# Douyin browser fallback

This branch adds an optional third Douyin resolver layer after the lightweight HTTP paths already in `main`.

Resolver order:

1. `iesdouyin.com/share/video/{id}` -> `window._ROUTER_DATA`
2. `www.douyin.com/video/{id}` -> server-rendered `self.__pace_f` / embedded media URLs
3. Optional headless Chrome/Chromium render -> rendered DOM -> `<video src>` / embedded media URLs
4. Existing Go CDN downloader with `.part` + HTTP Range resume

The browser layer is **disabled by default** so the normal Docker image does not need Chromium.

## Enable it

```env
DOUYIN_BROWSER_FALLBACK=true
DOUYIN_BROWSER_BIN=chromium
DOUYIN_BROWSER_TIMEOUT_SEC=45
DOUYIN_BROWSER_RENDER_MS=10000
DOUYIN_BROWSER_DOM_MAX_MB=24
DOUYIN_BROWSER_NO_SANDBOX=false
DOUYIN_BROWSER_PROFILE_DIR=
DOUYIN_BROWSER_EXTRA_ARGS=
```

`DOUYIN_BROWSER_BIN` can be an executable name available on `PATH` or an absolute path. If it is empty, VideoGet tries `chromium`, `chromium-browser`, `google-chrome`, `google-chrome-stable`, then `chrome`.

When `DOUYIN_BROWSER_PROFILE_DIR` is empty, VideoGet creates an isolated temporary browser profile and removes it after the attempt. A persistent/mounted profile can be supplied when a browser session is required, but it should be treated as sensitive because it can contain account cookies and other browser data.

`DOUYIN_BROWSER_NO_SANDBOX=true` should only be used inside an already isolated container/runtime where Chrome sandboxing cannot work.

## Docker

This PR intentionally does not install Chromium in the default image. The goal is to test the HTTP-only main path first and keep browser rendering as an opt-in fallback. A later change can add a separate browser-enabled image/profile if this fallback proves necessary in real Douyin failures.
