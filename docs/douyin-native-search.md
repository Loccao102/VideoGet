# Douyin native search

VideoGet uses a layered Douyin discovery flow:

1. public-index discovery as a cheap fast path;
2. Chromium/CDP native search when the public index does not fill the requested result count.

The native path opens `https://www.douyin.com/search/{keyword}?type=general` and lets Douyin's own page execute its current browser logic. VideoGet captures search responses and rendered video links instead of reconstructing Douyin signing/authentication outside the browser.

## Important: browser state is the source of truth

Current Douyin web requests may keep cookies in the browser's storage while a specific search XHR does **not** carry a visible `Cookie:` request header. Therefore VideoGet no longer treats the presence or absence of a Cookie header as proof of authentication.

The relevant state may be spread across browser-managed data such as:

- cookies;
- localStorage/sessionStorage;
- IndexedDB;
- service-worker/browser state;
- browser-generated headers/tokens/signatures;
- device/fingerprint state tied to the running browser.

VideoGet should not try to rebuild those values manually. The browser executes the request and VideoGet only observes the resulting network/DOM output.

## Preferred mode: attach to an already logged-in browser

Set:

```env
DOUYIN_CDP_URL=http://host.docker.internal:9222
```

When configured, VideoGet does **not** launch a fresh Chromium instance for Douyin search. It connects to the existing Chrome/Chromium DevTools endpoint and reuses that browser's complete origin state.

In remote-CDP mode:

- no Cookie header is required;
- `DOUYIN_COOKIE` is not injected;
- the connected browser keeps its own User-Agent/fingerprint;
- VideoGet opens/navigates a Douyin search tab and captures the browser's real responses;
- diagnostics never print cookie/storage values.

This is the most reliable mode when Douyin's current site keeps important state outside a manually copied Cookie header.

### Starting a dedicated Chrome session

Use a dedicated Chrome profile for VideoGet rather than your everyday browser profile. Example on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_douyin_chrome.ps1
```

Log in to Douyin once in that Chrome window. Keep it running while VideoGet searches.

The DevTools port grants powerful control over that dedicated browser session. Do not expose it to untrusted networks.

## Fallback mode: VideoGet-managed persistent profile

If `DOUYIN_CDP_URL` is empty, VideoGet launches Chromium itself and stores its profile under:

```env
DOUYIN_NATIVE_SEARCH_PROFILE_DIR=/app/downloads/.douyin-profile
```

This still preserves cookies, localStorage, IndexedDB and other browser-managed state across searches/restarts. `DOUYIN_COOKIE` remains only as a legacy bootstrap option for this internally launched browser; VideoGet does not assume search XHRs will send it.

## Search fallback order

1. intercept browser search JSON responses;
2. recursively extract `aweme_id`, `aweme_info`, or `aweme` from compatible response shapes;
3. harvest rendered `a[href*="/video/"]` links;
4. parse rendered DOM/hydration data.

This avoids depending on one fixed Douyin endpoint or one authentication header.

## Settings

```env
DOUYIN_NATIVE_SEARCH=true
DOUYIN_NATIVE_SEARCH_SCRIPT=/app/scripts/douyin_search_browser_v2.py
DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC=40
DOUYIN_NATIVE_SEARCH_RENDER_MS=9000
DOUYIN_NATIVE_SEARCH_SCROLLS=4
DOUYIN_NATIVE_SEARCH_MAX_PAGES=4
DOUYIN_NATIVE_SEARCH_DOM_MAX_MB=24

# Preferred when a logged-in Chrome is available:
DOUYIN_CDP_URL=http://host.docker.internal:9222

# Fallback managed profile:
DOUYIN_NATIVE_SEARCH_PROFILE_DIR=/app/downloads/.douyin-profile

DOUYIN_BROWSER_BIN=chromium
DOUYIN_BROWSER_NO_SANDBOX=true
```

## Diagnostics

When native search renders but finds no videos, the error includes safe state indicators:

- `session_source`: `remote-cdp`, `persistent-profile`, or `temporary-profile`;
- number of captured search responses;
- localStorage/sessionStorage key counts;
- whether IndexedDB is available;
- number of cookies stored in the browser.

Cookie/storage **values are never emitted**.

Do not interpret `cookies_stored=0` by itself as an authentication failure. The useful question is whether the connected browser can perform the same search interactively.

## Session reset

For the managed-profile mode:

```powershell
docker compose down
Remove-Item -Recurse -Force .\downloads\.douyin-profile -ErrorAction SilentlyContinue
docker compose up -d --build
```

For remote-CDP mode, close the dedicated Chrome, remove its dedicated profile directory only if you intentionally want a clean login, then start it again.

## Ollama keyword expansion

Ollama is optional for keyword expansion:

```env
KEYWORD_EXPANDER=ollama
KEYWORD_EXPANDER_STRICT=false
```

Set `KEYWORD_EXPANDER_STRICT=true` only if an Ollama outage should fail the search request.
