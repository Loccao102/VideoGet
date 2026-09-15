#!/usr/bin/env python3
"""Backward-compatible launcher for the Douyin Browser Bridge.

`douyin-cli` has been removed from VideoGet. This file only preserves the old
DOUYIN_BIN/douyin command name so existing docker-compose/.env files keep working.
All operations are delegated to `douyin_browser_bridge.py`.
"""
from pathlib import Path
import sys

# When this file is invoked through /usr/local/bin/douyin, __file__ may point at
# the symlink path. Resolve it so the real /app/scripts directory is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from douyin_browser_bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
