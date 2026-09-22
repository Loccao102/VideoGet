# Douyin native search

VideoGet uses a two-stage Douyin discovery flow:

1. public-index/Bing discovery as a cheap unauthenticated fast path;
2. Chromium/CDP native search when the public index does not fill the requested result count.

The native helper opens `https://www.douyin.com/search/{keyword}?type=general` and lets Douyin's own JavaScript create the signed browser requests. It no longer depends on one hard-coded search endpoint: it captures known `/aweme/.../search/...` responses plus compatible Douyin JSON search traffic, then falls back to rendered `/video/{aweme_id}` links and finally the DOM/hydration data.

## Authentication and persistent session

Douyin can return status `2483` (`请先登录，再继续搜索吧`) when there is no valid authenticated session.

VideoGet now prefers a persistent Chromium profile:

```env
DOUYIN_NATIVE_SEARCH_PROFILE_DIR=/app/downloads/.douyin-profile
```

The standard Docker configuration stores this profile inside the existing `./downloads:/app/downloads` bind mount, so cookies and browser-generated short-lived tokens can survive container restarts and can be refreshed by Douyin inside the browser context.

For the initial session, `DOUYIN_COOKIE` is still supported:

```env
DOUYIN_COOKIE=ttwid=...; sessionid=...; ...
```

Those values are injected into the browser before navigation. After successful searches, Douyin can update the persistent profile itself. Do not commit real cookies or browser profile data to Git.

Cookie **values are never printed** by the native-search helper. Diagnostics only expose cookie names/counts, whether the profile is persistent, and captured search endpoint URLs.

## Search fallback order

Native discovery is deliberately layered because Douyin changes page/API shapes frequently:

1. intercept browser search JSON responses;
2. recursively extract `aweme_id` / `aweme_info` / `aweme` from compatible response shapes;
3. harvest rendered `a[href*="/video/"]` links;
4. parse rendered DOM and hydration data.

This means an endpoint rename or response-wrapper change should not break all discovery paths at once.

## CDP settings

```env
DOUYIN_NATIVE_SEARCH=true
DOUYIN_NATIVE_SEARCH_SCRIPT=/app/scripts/douyin_search_browser_v2.py
DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC=40
DOUYIN_NATIVE_SEARCH_RENDER_MS=9000
DOUYIN_NATIVE_SEARCH_SCROLLS=4
DOUYIN_NATIVE_SEARCH_MAX_PAGES=4
DOUYIN_NATIVE_SEARCH_DOM_MAX_MB=24
DOUYIN_NATIVE_SEARCH_PROFILE_DIR=/app/downloads/.douyin-profile
DOUYIN_BROWSER_BIN=chromium
DOUYIN_BROWSER_NO_SANDBOX=true
```

The browser is used for request signing/session execution only. Search response JSON is parsed in Go. The older rendered-DOM parser remains a compatibility fallback.

## Troubleshooting

If search renders but returns no videos, the error now reports:

- browser cookie count;
- number of captured search responses;
- whether a persistent profile was used.

Useful interpretations:

- `cookies=0`: seed a fresh authenticated browser session;
- cookies present but `captured_search_responses=0`: Douyin likely changed the request surface again; DOM/video-link fallback will still be attempted;
- status `2483`: the current browser session is no longer authenticated.

When you rotate a leaked or expired Douyin session, stop VideoGet, remove `downloads/.douyin-profile`, provide a fresh local `DOUYIN_COOKIE`, and start the container again.

## Ollama keyword expansion

Ollama is optional for keyword expansion. By default an unavailable Ollama endpoint falls back to static Chinese keyword expansion without marking the search request as failed:

```env
KEYWORD_EXPANDER=ollama
KEYWORD_EXPANDER_STRICT=false
```

Set `KEYWORD_EXPANDER_STRICT=true` only if an Ollama outage should be surfaced as an API error. If you want Ollama-backed expansion, make sure Ollama is actually listening at `OLLAMA_BASE_URL` and reachable from the container.
