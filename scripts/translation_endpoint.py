#!/usr/bin/env python3
"""Translation-provider connectivity preflight for localization jobs.

This intentionally performs a cheap provider health request before an expensive
translation pass. It keeps network/config failures from being retried as if they
were per-scene translation failures.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request


def _clean(value: object) -> str:
    return str(value or "").strip()


def _provider() -> str:
    value = _clean(os.getenv("TRANSLATE_PROVIDER", "ollama")).lower()
    if value == "openai_compatible":
        return "openai"
    return value or "ollama"


def _timeout() -> float:
    try:
        return max(1.0, float(os.getenv("TRANSLATE_PREFLIGHT_TIMEOUT_SEC", "5")))
    except ValueError:
        return 5.0


def endpoint_info() -> dict:
    provider = _provider()
    if provider == "ollama":
        base = _clean(os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")).rstrip("/")
        return {
            "provider": provider,
            "baseUrl": base,
            "healthUrl": base + "/api/tags",
            "headers": {},
        }
    if provider == "openai":
        base = _clean(os.getenv("OPENAI_COMPAT_BASE_URL", "http://host.docker.internal:11434/v1")).rstrip("/")
        api_key = _clean(os.getenv("OPENAI_COMPAT_API_KEY", ""))
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return {
            "provider": provider,
            "baseUrl": base,
            "healthUrl": base + "/models",
            "headers": headers,
        }
    raise RuntimeError(f"unsupported TRANSLATE_PROVIDER={provider!r}")


def _host_details(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if not host:
            return ""
        values = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ips: list[str] = []
        for value in values:
            address = str(value[4][0])
            if address not in ips:
                ips.append(address)
        return f" host={host} resolved={','.join(ips[:6])}" if ips else f" host={host}"
    except Exception as error:
        return f" dns={error}"


def require_translation_endpoint() -> dict:
    info = endpoint_info()
    request = urllib.request.Request(info["healthUrl"], headers=info["headers"], method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
            # Validate that the endpoint is at least returning JSON. The exact
            # schema differs between Ollama and OpenAI-compatible providers.
            if raw.strip():
                try:
                    json.loads(raw)
                except json.JSONDecodeError as error:
                    raise RuntimeError(
                        f"translation endpoint returned non-JSON health response from {info['healthUrl']}"
                    ) from error
            return info
    except urllib.error.HTTPError as error:
        # A reachable endpoint with an auth/config error is still a useful,
        # actionable failure and should not be retried scene-by-scene.
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"translation endpoint reachable but health check returned HTTP {error.code}: {body}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        detail = _host_details(info["healthUrl"])
        hint = ""
        if info["provider"] == "ollama" and "host.docker.internal" in info["baseUrl"]:
            hint = (
                " Docker cannot reach Ollama on the host. On Windows, confirm Ollama is running, "
                "set user environment variable OLLAMA_HOST=0.0.0.0:11434, fully quit/restart Ollama, "
                "then test from the container: curl http://host.docker.internal:11434/api/tags."
            )
        raise RuntimeError(
            f"translation provider unavailable: {info['healthUrl']} ({error}).{detail}{hint}"
        ) from error
