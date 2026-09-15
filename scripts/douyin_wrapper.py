#!/usr/bin/env python3
"""Thin douyin-cli launcher used by VideoGet.

Search may intentionally use DOUYIN_COOKIE from the environment. Downloads are more
sensitive: a manually pasted cookie can be incomplete (notably missing UIFID) and
can override the richer session persisted by `douyin auth cookie-login`.

For `-t aweme` we therefore prefer douyin-cli's persisted config by removing the
environment cookie unless DOUYIN_DOWNLOAD_USE_ENV_COOKIE=true is explicitly set.
This does not bypass Douyin verification/risk controls; it only avoids shadowing a
valid user-authenticated CLI session with a stale/incomplete environment cookie.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

DEFAULT_REAL_DOUYIN = "/usr/local/bin/douyin-real"


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def task_type(args: list[str]) -> str:
    try:
        index = args.index("-t")
        return args[index + 1].strip().lower()
    except (ValueError, IndexError):
        return ""


def real_binary() -> str:
    configured = os.getenv("DOUYIN_REAL_BIN", DEFAULT_REAL_DOUYIN).strip() or DEFAULT_REAL_DOUYIN
    try:
        configured_path = Path(configured).resolve()
        self_path = Path(sys.argv[0]).resolve()
        # Protect old compose/.env values such as DOUYIN_REAL_BIN=douyin from
        # recursively invoking this wrapper after it becomes the default command.
        if configured_path == self_path and Path(DEFAULT_REAL_DOUYIN).exists():
            return DEFAULT_REAL_DOUYIN
    except OSError:
        pass
    return configured


def main() -> int:
    args = sys.argv[1:]
    env = os.environ.copy()
    kind = task_type(args)
    removed_cookie = False
    if kind == "aweme" and not truthy(env.get("DOUYIN_DOWNLOAD_USE_ENV_COOKIE")):
        removed_cookie = bool(env.pop("DOUYIN_COOKIE", None))

    process = subprocess.run(
        [real_binary(), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if process.stdout:
        sys.stdout.write(process.stdout)
    if process.stderr:
        sys.stderr.write(process.stderr)

    combined = (process.stdout + "\n" + process.stderr).lower()
    if process.returncode != 0 and (
        "uifid not found" in combined or "argussecurityplugin" in combined
    ):
        source = "persisted CLI session" if removed_cookie else "current Douyin session"
        sys.stderr.write(
            "\nVideoGet: Douyin rejected the %s because UIFID/session data is missing. "
            "This is an authentication/risk-control failure, not a media/TTS error.\n"
            "Re-authenticate manually inside the container with:\n"
            "  docker compose exec videoget douyin auth cookie-login\n"
            "Then retry the existing job. VideoGet does not bypass Douyin verification.\n"
            % source
        )
    return int(process.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
