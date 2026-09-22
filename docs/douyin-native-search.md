# Douyin native search

VideoGet uses a two-stage Douyin discovery flow:

1. public-index/Bing discovery as a cheap unauthenticated fast path;
2. Chromium/CDP native search when the public index does not fill the requested result count.

The CDP helper opens `https://www.douyin.com/search/{keyword}?type=general` and lets Douyin's own JavaScript create the signed requests. It no longer depends on one fixed API path: it captures Douyin search/aweme JSON responses, recursively extracts `aweme_id`/nested `aweme_info` shapes, then falls back to rendered `/video/{id}` links and the bounded DOM snapshot.

## Authentication

Douyin native keyword search can return status `2483` (`请先登录，再继续搜索吧`) when there is no valid authenticated session. Configure a fresh browser Cookie header:

```env
DOUYIN_COOKIE=ttwid=...; sessionid=...; msToken=...; ...
```

The helper injects those cookies into `.douyin.com` before navigating. In the standard Docker setup the Chromium profile is persistent at `/app/downloads/.douyin-profile`, so Douyin-generated browser state/local storage can survive subsequent searches. You can override it with `DOUYIN_NATIVE_SEARCH_PROFILE_DIR`. Cookie values are never written to diagnostic output.

Do not commit real cookies to Git. Keep them in the local `.env` only.

## CDP settings

```env
DOUYIN_NATIVE_SEARCH=true
DOUYIN_NATIVE_SEARCH_SCRIPT=/app/scripts/douyin_search_browser_v2.py
DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC=45
DOUYIN_NATIVE_SEARCH_RENDER_MS=12000
DOUYIN_NATIVE_SEARCH_SCROLLS=4
DOUYIN_NATIVE_SEARCH_MAX_PAGES=5
DOUYIN_NATIVE_SEARCH_DOM_MAX_MB=32
DOUYIN_BROWSER_BIN=chromium
DOUYIN_BROWSER_NO_SANDBOX=true
```

The browser is used for request signing/session execution only. Search response JSON is parsed in Go. The older rendered-DOM parser remains a compatibility fallback.

## Ollama keyword expansion

Ollama is optional for keyword expansion. By default an unavailable Ollama endpoint falls back to static Chinese keyword expansion without marking the search request as failed:

```env
KEYWORD_EXPANDER=ollama
KEYWORD_EXPANDER_STRICT=false
```

Set `KEYWORD_EXPANDER_STRICT=true` only if an Ollama outage should be surfaced as an API error. If you want Ollama-backed expansion, make sure Ollama is actually listening at `OLLAMA_BASE_URL` and reachable from the container.
