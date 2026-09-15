#!/usr/bin/env python3
"""Auth-aware douyin-cli launcher used by VideoGet.

Search may intentionally use DOUYIN_COOKIE from the environment. Aweme downloads
prefer the session persisted by `douyin auth cookie-login`, because pasted cookies
can be stale or incomplete. If that persisted session is rejected specifically by
Argus for missing UIFID, the wrapper may retry exactly once with DOUYIN_COOKIE.

Some browser exports expose UIFID_TEMP but omit UIFID from the copied cookie string.
When DOUYIN_UIFID is supplied from the same authenticated browser/session, VideoGet
may append it as `UIFID=<value>` to DOUYIN_COOKIE only when UIFID is otherwise
missing. It never derives UIFID from UIFID_TEMP and never overwrites an existing
UIFID cookie.

This wrapper does not generate tokens, solve verification, or bypass Douyin risk
controls. It only chooses/combines user-provided authenticated session material.
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


def has_cookie_key(cookie: str | None, key: str) -> bool:
    wanted = key.strip().lower()
    if not wanted:
        return False
    for part in str(cookie or "").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name.strip().lower() == wanted and value.strip():
            return True
    return False


def augment_cookie_with_uifid(cookie: str | None, uifid: str | None) -> tuple[str, bool]:
    """Append UIFID only when a real cookie exists and UIFID is absent.

    A standalone UIFID is intentionally not treated as an authenticated session,
    and UIFID_TEMP is never promoted/renamed to UIFID.
    """
    value = str(cookie or "").strip()
    explicit_uifid = str(uifid or "").strip()
    if not value or not explicit_uifid or has_cookie_key(value, "UIFID"):
        return value, False
    return value.rstrip("; ") + "; UIFID=" + explicit_uifid, True


def argus_uifid_failure(stdout: str, stderr: str) -> bool:
    combined = (stdout + "\n" + stderr).lower()
    return "uifid not found" in combined or "argussecurityplugin" in combined


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


def run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [real_binary(), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )


def emit(process: subprocess.CompletedProcess[str]) -> None:
    if process.stdout:
        sys.stdout.write(process.stdout)
    if process.stderr:
        sys.stderr.write(process.stderr)


def env_with_cookie(base: dict[str, str], cookie: str) -> dict[str, str]:
    env = base.copy()
    if cookie:
        env["DOUYIN_COOKIE"] = cookie
    else:
        env.pop("DOUYIN_COOKIE", None)
    return env


def main() -> int:
    args = sys.argv[1:]
    original_env = os.environ.copy()
    original_cookie = original_env.get("DOUYIN_COOKIE", "")
    explicit_uifid = original_env.get("DOUYIN_UIFID", "")
    effective_cookie, augmented_uifid = augment_cookie_with_uifid(original_cookie, explicit_uifid)
    kind = task_type(args)
    prefer_env_cookie = truthy(original_env.get("DOUYIN_DOWNLOAD_USE_ENV_COOKIE"))

    # Non-aweme commands keep the existing environment behavior unchanged.
    if kind != "aweme":
        process = run_cli(args, original_env)
        emit(process)
        return int(process.returncode)

    # Optional explicit override: use the environment cookie directly. If the
    # exported cookie only had UIFID_TEMP, DOUYIN_UIFID may safely supplement it.
    if prefer_env_cookie:
        process = run_cli(args, env_with_cookie(original_env, effective_cookie))
        emit(process)
        if process.returncode != 0 and argus_uifid_failure(process.stdout, process.stderr):
            sys.stderr.write(
                "\nVideoGet: Douyin rejected the environment cookie with an Argus/UIFID error. "
                "Refresh the authenticated browser cookie/UIFID or run `douyin auth cookie-login`; "
                "VideoGet does not bypass Douyin verification.\n"
            )
        return int(process.returncode)

    # Default: try the persisted CLI login without allowing a possibly stale
    # DOUYIN_COOKIE to shadow it.
    persisted_env = original_env.copy()
    persisted_env.pop("DOUYIN_COOKIE", None)
    persisted_env.pop("DOUYIN_UIFID", None)
    first = run_cli(args, persisted_env)
    if first.returncode == 0:
        emit(first)
        return 0

    # Only this precise auth failure is eligible for one fallback. We never
    # retry generic 403s, captchas, signature errors, or other risk controls.
    if argus_uifid_failure(first.stdout, first.stderr) and has_cookie_key(effective_cookie, "UIFID"):
        if augmented_uifid:
            sys.stderr.write(
                "VideoGet: persisted Douyin session is missing UIFID; retrying once with "
                "DOUYIN_COOKIE supplemented by DOUYIN_UIFID from the local environment.\n"
            )
        else:
            sys.stderr.write(
                "VideoGet: persisted Douyin session is missing UIFID; retrying once with "
                "the authenticated DOUYIN_COOKIE from the local environment.\n"
            )
        second = run_cli(args, env_with_cookie(original_env, effective_cookie))
        emit(second)
        if second.returncode == 0:
            return 0
        if argus_uifid_failure(second.stdout, second.stderr):
            sys.stderr.write(
                "\nVideoGet: both the persisted session and the environment cookie were rejected "
                "by Argus/UIFID checks. Refresh the browser login/session and retry. "
                "VideoGet does not bypass Douyin verification.\n"
            )
        return int(second.returncode)

    emit(first)
    if argus_uifid_failure(first.stdout, first.stderr):
        if not original_cookie:
            fallback_note = "DOUYIN_COOKIE is not configured in the container"
        elif has_cookie_key(original_cookie, "UIFID"):
            fallback_note = "DOUYIN_COOKIE contains UIFID but was not eligible for fallback"
        elif explicit_uifid:
            fallback_note = "DOUYIN_UIFID is set but could not form a usable authenticated cookie"
        else:
            fallback_note = "DOUYIN_COOKIE has no UIFID and DOUYIN_UIFID is not configured"
        sys.stderr.write(
            "\nVideoGet: Douyin rejected the persisted CLI session because UIFID/session "
            "data is missing. %s, so no environment-cookie fallback was attempted.\n"
            "Refresh/login to Douyin and either run:\n"
            "  docker compose exec videoget douyin auth cookie-login\n"
            "or put a fresh authenticated cookie in local .env as DOUYIN_COOKIE. If that browser "
            "export contains only UIFID_TEMP, also set DOUYIN_UIFID from the same browser/session. "
            "Never commit either value.\n"
            % fallback_note
        )
    return int(first.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
