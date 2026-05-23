# LLM: API JSON HTTP fetching is isolated from checkpoint shaping.
# 模块用途: 执行带限流重试的 JSON GET，并返回 hash/status/json 结构。

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterable

from ..contracts.gates import NetworkResolver, NetworkSafetyFacts, evaluate_network_safety_gate


# LLM: fetch_json performs one bounded GET with one structured rate-limit retry.
# 函数用途: 对 403/429 的 Retry-After/X-RateLimit-Reset 做一次有限等待，避免模型退化成手写数据。
def fetch_json(
    url: str,
    *,
    timeout: int,
    resolver: NetworkResolver | None = None,
    allowed_private_hosts: Iterable[str] = (),
    allow_private_resolution: bool | None = None,
) -> dict[str, object]:
    _ensure_network_safe(
        url,
        resolver or _default_network_resolver,
        allowed_private_hosts,
        allow_private_resolution=allow_private_resolution,
    )
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "SimplePythonAgent/1.0"})
    try:
        return _fetch_json_once(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        delay = _rate_limit_retry_delay(exc)
        if delay is None:
            raise ValueError(f"API_HTTP_ERROR: HTTP {exc.code}") from exc
        return _fetch_after_rate_limit_retry(req, timeout=timeout, delay=delay)
    except urllib.error.URLError as exc:
        raise ValueError(f"API_REQUEST_FAILED: {exc.__class__.__name__}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"API_JSON_INVALID: {exc.msg}") from exc


def _default_network_resolver(host: str) -> tuple[str, ...]:
    answers = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return tuple(str(sockaddr[0]) for *_prefix, sockaddr in answers)


def _ensure_network_safe(
    url: str,
    resolver: NetworkResolver,
    allowed_private_hosts: Iterable[str],
    *,
    allow_private_resolution: bool | None = None,
) -> None:
    decision = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            url=url,
            resolver=resolver,
            allowed_private_hosts=allowed_private_hosts,
            allow_private_resolution=_effective_allow_private_resolution(allow_private_resolution),
        )
    )
    if decision.allowed:
        return
    code = decision.finding_codes[0] if decision.finding_codes else "NETWORK_SAFETY_DENIED"
    raise ValueError(f"{code}: network safety gate denied API JSON request")


def _effective_allow_private_resolution(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.environ.get("MY_AGENT_ALLOW_PRIVATE_URLS", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _fetch_json_once(req: urllib.request.Request, *, timeout: int) -> dict[str, object]:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        value = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(value, (dict, list)):
            raise ValueError("API_JSON_INVALID: response JSON must be object or array")
        return {"json": value, "status": getattr(resp, "status", 0), "sha256": hashlib.sha256(raw).hexdigest()}


def _fetch_after_rate_limit_retry(req: urllib.request.Request, *, timeout: int, delay: float) -> dict[str, object]:
    time.sleep(delay)
    try:
        return _fetch_json_once(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"API_HTTP_ERROR: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"API_REQUEST_FAILED: {exc.__class__.__name__}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"API_JSON_INVALID: {exc.msg}") from exc


def _rate_limit_retry_delay(exc: urllib.error.HTTPError) -> float | None:
    if exc.code not in {403, 429}:
        return None
    retry_after = _positive_float(exc.headers.get("Retry-After"))
    if retry_after is not None:
        return retry_after if retry_after <= 90 else None
    reset_epoch = _positive_float(exc.headers.get("X-RateLimit-Reset"))
    if reset_epoch is None:
        return None
    delay = max(0.0, reset_epoch - time.time() + 1.0)
    return delay if delay <= 90 else None


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value) if value not in (None, "") else -1.0
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


__all__ = ["fetch_json"]
