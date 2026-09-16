# Douyin native search and browser fallback

VideoGet keeps the cheap public-index lookup as the first discovery step, but it no longer depends on Bing alone. When the public index returns fewer items than the requested `limit`, VideoGet opens Douyin's own search page in headless Chromium and fills the remaining results from the rendered Douyin page/hydration data.

## Search order

1. Public index fast path: `site:douyin.com/video <keyword>`
2. If fewer than `limit` videos were found and `DOUYIN_NATIVE_SEARCH=true`:
   - open `https://www.douyin.com/search/{keyword}?type=general`
   - let Chromium execute Douyin's normal web application
   - read rendered `/video/{aweme_id}` links and `self.__pace_f` / hydrated `aweme_id` data
   - extract available title, author, duration, publish time and engagement counters
3. Deduplicate by `aweme_id`; public-index results keep their order and native results fill the remainder.

This avoids reimplementing Douyin's A-Bogus/X-Bogus signing logic. The browser itself executes the same search surface a normal Douyin web user opens.

## Download order

1. `iesdouyin.com/share/video/{id}` -> `window._ROUTER_DATA`
2. `www.douyin.com/video/{id}` -> server-rendered `self.__pace_f` / embedded media URLs
3. If those fail and `DOUYIN_BROWSER_FALLBACK=true`: headless Chromium -> rendered DOM -> media URL
4. Existing Go CDN downloader with `.part` + HTTP Range resume

## Default Docker configuration

The standard Docker image now includes Debian Chromium because native Douyin search is enabled by default.

```env
DOUYIN_NATIVE_SEARCH=true
DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC=45
DOUYIN_NATIVE_SEARCH_RENDER_MS=12000
DOUYIN_NATIVE_SEARCH_DOM_MAX_MB=32
DOUYIN_NATIVE_SEARCH_PROFILE_DIR=

DOUYIN_BROWSER_FALLBACK=true
DOUYIN_BROWSER_BIN=chromium
DOUYIN_BROWSER_TIMEOUT_SEC=45
DOUYIN_BROWSER_RENDER_MS=10000
DOUYIN_BROWSER_DOM_MAX_MB=24
DOUYIN_BROWSER_NO_SANDBOX=true
DOUYIN_BROWSER_PROFILE_DIR=
DOUYIN_BROWSER_EXTRA_ARGS=
```

`DOUYIN_BROWSER_BIN` can be an executable name available on `PATH` or an absolute path. If empty, VideoGet tries `chromium`, `chromium-browser`, `google-chrome`, `google-chrome-stable`, then `chrome`.

When a browser profile directory is empty, VideoGet creates an isolated temporary profile and removes it afterwards. You can provide a persistent/mounted profile when Douyin requires a browser session. Treat that directory as sensitive because it can contain account cookies and other browser data.

Native browser searches are serialized to avoid Chrome profile lock conflicts and to keep resource usage predictable when keyword expansion produces several Chinese search terms.

`DOUYIN_BROWSER_NO_SANDBOX=true` is intended for the isolated Docker container where Chromium runs as root. If you run VideoGet directly on a desktop, you can set it back to `false`.
