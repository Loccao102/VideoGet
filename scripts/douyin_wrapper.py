#!/usr/bin/env python3
"""Auth-aware douyin-cli launcher used by VideoGet.

Search uses the configured environment as-is. Aweme downloads prefer a fresh,
complete browser cookie from DOUYIN_COOKIE when it contains both UIFID and a
recognizable login/session key. This avoids silently discarding a valid browser
session in favor of an older persisted CLI login.

If both the browser cookie and persisted douyin-cli login are rejected specifically
by Argus/UIFID, VideoGet may try yt-dlp once with the same user-provided browser
cookie. This is an alternate extractor only: the wrapper never generates tokens,
solves verification, or bypasses Douyin risk controls.

DOUYIN_UIFID remains an optional compatibility aid for browser exports that truly
lack UIFID: it may append UIFID to an existing DOUYIN_COOKIE, but never promotes
UIFID_TEMP, never overwrites an existing UIFID, and never treats UIFID alone as a
login session.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

DEFAULT_REAL_DOUYIN = "/usr/local/bin/douyin-real"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def task_type(args: list[str]) -> str:
    try:
        index = args.index("-t")
        return args[index + 1].strip().lower()
    except (ValueError, IndexError):
        return ""


def option_value(args: list[str], name: str) -> str:
    try:
        index = args.index(name)
        return args[index + 1].strip()
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


def has_login_session(cookie: str | None) -> bool:
    return any(
        has_cookie_key(cookie, key)
        for key in ("sessionid", "sessionid_ss", "sid_tt", "ttwid")
    )


def complete_browser_cookie(cookie: str | None) -> bool:
    return has_cookie_key(cookie, "UIFID") and has_login_session(cookie)


def augment_cookie_with_uifid(cookie: str | None, uifid: str | None) -> tuple[str, bool]:
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


def persisted_env(base: dict[str, str]) -> dict[str, str]:
    env = base.copy()
    env.pop("DOUYIN_COOKIE", None)
    env.pop("DOUYIN_UIFID", None)
    return env


def write_netscape_cookie_file(cookie: str) -> str:
    """Write the user-provided Douyin cookie to a short-lived yt-dlp cookie file.

    Using a temporary cookie file avoids exposing the complete session string in
    the process command line. The file is removed immediately after yt-dlp exits.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="videoget-douyin-", suffix=".cookies", delete=False
    )
    try:
        os.chmod(handle.name, 0o600)
        handle.write("# Netscape HTTP Cookie File\n")
        for part in str(cookie or "").split(";"):
            name, separator, value = part.strip().partition("=")
            name = name.strip().replace("\t", "").replace("\r", "").replace("\n", "")
            value = value.strip().replace("\t", "").replace("\r", "").replace("\n", "")
            if not separator or not name:
                continue
            handle.write(f".douyin.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}\n")
        return handle.name
    finally:
        handle.close()


def try_ytdlp_fallback(
    args: list[str], env: dict[str, str], cookie: str
) -> subprocess.CompletedProcess[str] | None:
    if not truthy(env.get("DOUYIN_YTDLP_FALLBACK", "true")):
        return None
    binary = shutil.which(env.get("DOUYIN_YTDLP_BIN", "yt-dlp"))
    raw_url = option_value(args, "-u")
    output_dir = option_value(args, "-p")
    if not binary or not raw_url or not output_dir or not cookie:
        return None

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    user_agent = env.get("DOUYIN_USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT
    template = str(Path(output_dir) / "douyin-ytdlp [%(id)s].%(ext)s")
    cookie_file = write_netscape_cookie_file(cookie)
    try:
        command = [
            binary,
            "--ignore-config",
            "--no-playlist",
            "--no-progress",
            "--no-continue",
            "--retries", "2",
            "--fragment-retries", "2",
            "--socket-timeout", "25",
            "--merge-output-format", "mp4",
            "--referer", "https://www.douyin.com/",
            "--user-agent", user_agent,
            "--cookies", cookie_file,
            "--print", "after_move:filepath",
            "-o", template,
            raw_url,
        ]
        return subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    finally:
        try:
            os.remove(cookie_file)
        except OSError:
            pass


def try_argus_fallback(args: list[str], env: dict[str, str], cookie: str) -> int | None:
    if not complete_browser_cookie(cookie):
        return None
    sys.stderr.write(
        "VideoGet: both douyin-cli auth sources hit Argus/UIFID; trying yt-dlp once "
        "with the same authenticated browser cookie.\n"
    )
    process = try_ytdlp_fallback(args, env, cookie)
    if process is None:
        sys.stderr.write(
            "VideoGet: yt-dlp fallback is unavailable or disabled; no further automatic "
            "Douyin download attempts will be made.\n"
        )
        return None
    emit(process)
    if process.returncode == 0:
        sys.stderr.write("VideoGet: yt-dlp fallback downloaded the Douyin media successfully.\n")
        return 0
    sys.stderr.write(
        "VideoGet: yt-dlp fallback also failed. The current browser/session context is "
        "not accepted for this download; refresh the login/session before retrying.\n"
    )
    return int(process.returncode)


def main() -> int:
    args = sys.argv[1:]
    original_env = os.environ.copy()
    original_cookie = original_env.get("DOUYIN_COOKIE", "")
    explicit_uifid = original_env.get("DOUYIN_UIFID", "")
    effective_cookie, augmented_uifid = augment_cookie_with_uifid(original_cookie, explicit_uifid)
    kind = task_type(args)
    force_env_cookie = truthy(original_env.get("DOUYIN_DOWNLOAD_USE_ENV_COOKIE"))
    env_cookie_complete = complete_browser_cookie(effective_cookie)

    if kind != "aweme":
        process = run_cli(args, original_env)
        emit(process)
        return int(process.returncode)

    # A complete browser cookie is the preferred auth source for aweme downloads.
    if force_env_cookie or env_cookie_complete:
        if env_cookie_complete:
            sys.stderr.write(
                "VideoGet: using authenticated DOUYIN_COOKIE for Douyin aweme download "
                "(UIFID + login session detected).\n"
            )
        elif force_env_cookie:
            sys.stderr.write(
                "VideoGet: DOUYIN_DOWNLOAD_USE_ENV_COOKIE=true; using DOUYIN_COOKIE even "
                "though VideoGet could not verify UIFID + login-session keys locally.\n"
            )

        first = run_cli(args, env_with_cookie(original_env, effective_cookie))
        if first.returncode == 0:
            emit(first)
            return 0

        # Only an Argus/UIFID rejection may fall back to the persisted login.
        if argus_uifid_failure(first.stdout, first.stderr):
            sys.stderr.write(
                "VideoGet: environment cookie was rejected by Argus/UIFID; trying the "
                "persisted douyin-cli login once.\n"
            )
            second = run_cli(args, persisted_env(original_env))
            emit(second)
            if second.returncode == 0:
                return 0
            if argus_uifid_failure(second.stdout, second.stderr):
                fallback_code = try_argus_fallback(args, original_env, effective_cookie)
                if fallback_code is not None:
                    return fallback_code
                sys.stderr.write(
                    "\nVideoGet: both the browser cookie and persisted CLI session were "
                    "rejected by Argus/UIFID checks. The cookie may be valid in the browser "
                    "but not accepted for this signed web request/session context. Refresh "
                    "the browser login/cookie or run `douyin auth cookie-login`; VideoGet "
                    "does not bypass Douyin verification.\n"
                )
            return int(second.returncode)

        emit(first)
        return int(first.returncode)

    # No complete browser cookie is available: use the persisted CLI session first.
    first = run_cli(args, persisted_env(original_env))
    if first.returncode == 0:
        emit(first)
        return 0

    # If we can form a complete environment cookie only via DOUYIN_UIFID, try it once.
    if argus_uifid_failure(first.stdout, first.stderr) and complete_browser_cookie(effective_cookie):
        if augmented_uifid:
            sys.stderr.write(
                "VideoGet: persisted session is missing UIFID; retrying once with "
                "DOUYIN_COOKIE supplemented by DOUYIN_UIFID.\n"
            )
        second = run_cli(args, env_with_cookie(original_env, effective_cookie))
        emit(second)
        if second.returncode == 0:
            return 0
        if argus_uifid_failure(second.stdout, second.stderr):
            fallback_code = try_argus_fallback(args, original_env, effective_cookie)
            if fallback_code is not None:
                return fallback_code
        return int(second.returncode)

    emit(first)
    if argus_uifid_failure(first.stdout, first.stderr):
        if not original_cookie:
            note = "DOUYIN_COOKIE is not configured in the container"
        elif not has_cookie_key(effective_cookie, "UIFID"):
            note = "the environment cookie has no UIFID"
        elif not has_login_session(effective_cookie):
            note = "the environment cookie has UIFID but no recognized login-session key"
        else:
            note = "the environment cookie was not considered usable"
        sys.stderr.write(
            "\nVideoGet: Douyin rejected the persisted CLI session. %s. Put the fresh "
            "authenticated browser cookie in local .env as DOUYIN_COOKIE, recreate the "
            "container, and retry. Never commit the cookie.\n" % note
        )
    return int(first.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
