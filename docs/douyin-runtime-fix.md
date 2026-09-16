# Douyin search runtime hardening

This patch addresses two production failures observed after the CDP native-search rollout:

1. Chromium profile cleanup could raise `Errno 39: Directory not empty` after a successful capture because renderer/network child processes were still touching the temporary profile.
2. Keyword expansion could launch up to eight serialized Douyin browser searches, making one `/api/search` request appear to hang when a fallback timed out.

Runtime changes:

- `scripts/douyin_search_browser_v2.py` launches Chromium in its own process group and terminates the whole group before cleanup.
- Temporary profile deletion is best-effort and can no longer overwrite a successful search result.
- Chromium stderr uses `DEVNULL` so a noisy browser cannot fill a pipe and block the helper.
- Default native search render time is 8 seconds, with 2 scrolls and 3 captured pages.
- Native search helper timeout is 25 seconds.
- `SEARCH_KEYWORD_LIMIT` defaults to 4 and limits both static and Ollama-expanded keyword lists.
- Docker and Compose use the hardened v2 helper by default.

A fresh logged-in `DOUYIN_COOKIE` may still be required by Douyin. Authentication or risk-control failures should now surface as explicit search errors instead of cleanup exceptions or long silent waits.
