
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .errors import (
    ProviderContextWindowError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
)

_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429, 502, 503, 504, 529})
_RETRYABLE_HTTP_DELAYS_SECONDS = (2.0, 5.0, 15.0)
_MAX_RETRY_AFTER_SECONDS = 30.0
_RETRYABLE_NETWORK_ERROR_MARKERS = frozenset(
    {
        "unexpected_eof",
        "unexpected eof",
        "eof occurred",
        "remote end closed",
        "remote disconnected",
        "tunnel connection failed",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "connection reset",
        "connection aborted",
        "broken pipe",
        "temporarily unavailable",
    }
)
_CONTEXT_WINDOW_HTTP_STATUS_CODES = frozenset({400, 413, 422})
_CONTEXT_WINDOW_ERROR_MARKERS = frozenset(
    {
        "context length",
        "context_length",
        "context window",
        "context_window",
        "maximum context",
        "max context",
        "input too long",
        "prompt too long",
        "too many tokens",
        "token limit",
    }
)


@dataclass(frozen=True)
class GatewayRequest:
    """Immutable request envelope for one provider HTTP call."""

    api_base: str
    api_key: str
    path: str
    payload: dict[str, Any]
    headers: dict[str, str]
    timeout: int

    @property
    def url(self) -> str:
        """Return the final endpoint after joining provider base URL and API path."""
        return self.api_base + self.path


def post_json(
    request: GatewayRequest,
) -> dict[str, Any]:
    """POST JSON and normalize provider/network failures into typed exceptions."""
    _require_api_key(request.api_key)
    try:
        with _open_gateway_request(request) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _runtime_network_error(exc, request) from exc
    # decode/loads 在 with 外做:坏字节(非 UTF-8)或非 JSON 响应体不能漏出去崩整轮,
    # 归一为可恢复的 ProviderResponseError(适配器无法解析,不是任务本身的 bug)。
    try:
        text = raw.decode("utf-8", "replace")
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _runtime_decode_error(exc, request) from exc


def post_stream(
    request: GatewayRequest,
) -> list[str]:
    """POST a streaming request and collect SSE data lines."""
    return list(_post_stream_lines(request))


def post_stream_iter(
    request: GatewayRequest,
):
    """POST a streaming request and yield normalized SSE data lines."""
    yield from _post_stream_lines(request)


def _post_stream_lines(request: GatewayRequest) -> Iterator[str]:
    """Shared streaming implementation used by list and iterator callers."""
    request.payload["stream"] = True
    _require_api_key(request.api_key)
    deadline = _stream_deadline(request.timeout)
    try:
        with _open_gateway_request(request) as resp:
            yield from _iter_sse_data_lines(resp, deadline=deadline, timeout=request.timeout, url=request.url)
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _runtime_network_error(exc, request) from exc


def _urllib_request(request: GatewayRequest) -> urllib.request.Request:
    """Build the urllib request without exposing shell/string transport paths."""
    return urllib.request.Request(
        request.url,
        data=json.dumps(request.payload).encode("utf-8"),
        method="POST",
        headers=request.headers,
    )


def _open_gateway_request(request: GatewayRequest):
    last_attempt = len(_RETRYABLE_HTTP_DELAYS_SECONDS)
    for attempt in range(last_attempt + 1):
        response = _gateway_request_attempt(request, attempt, last_attempt)
        if response is not None:
            return response
    raise RuntimeError("unreachable gateway retry state")


def _gateway_request_attempt(request: GatewayRequest, attempt: int, last_attempt: int):
    req = _urllib_request(request)
    try:
        return urllib.request.urlopen(req, timeout=request.timeout)
    except urllib.error.HTTPError as exc:
        if not _should_retry_http_error(exc, attempt, last_attempt):
            raise
        time.sleep(_retry_delay_seconds(exc, attempt))
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if not _should_retry_network_error(exc, attempt, last_attempt):
            raise
        time.sleep(_network_retry_delay_seconds(attempt))
        return None


def _should_retry_http_error(exc: urllib.error.HTTPError, attempt: int, last_attempt: int) -> bool:
    return attempt < last_attempt and int(getattr(exc, "code", 0) or 0) in _RETRYABLE_HTTP_STATUS_CODES


def _should_retry_network_error(exc: BaseException, attempt: int, last_attempt: int) -> bool:
    return attempt < last_attempt and _is_transient_network_error(exc)


def _retry_delay_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    header_delay = _retry_after_header_seconds(exc)
    if header_delay is not None:
        return min(header_delay, _MAX_RETRY_AFTER_SECONDS)
    index = min(attempt, len(_RETRYABLE_HTTP_DELAYS_SECONDS) - 1)
    return _RETRYABLE_HTTP_DELAYS_SECONDS[index]


def _network_retry_delay_seconds(attempt: int) -> float:
    index = min(attempt, len(_RETRYABLE_HTTP_DELAYS_SECONDS) - 1)
    return _RETRYABLE_HTTP_DELAYS_SECONDS[index]


def _retry_after_header_seconds(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers is not None and hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return max(0.0, value)


def _require_api_key(api_key: str) -> None:
    """Fail fast for missing credentials before opening a network connection."""
    if not api_key:
        raise ValueError("api_key 为空：请在配置文件中填写 API Key。")


def _runtime_http_error(exc: urllib.error.HTTPError) -> RuntimeError:
    """Classify provider HTTP errors at the backend boundary."""
    detail = exc.read().decode("utf-8", "replace")
    code = int(getattr(exc, "code", 0) or 0)
    if code in _CONTEXT_WINDOW_HTTP_STATUS_CODES and _provider_error_indicates_context_window(detail):
        return ProviderContextWindowError(
            f"HTTP {exc.code}: {detail}",
            details={"status_code": code, "provider_error": _provider_error_payload(detail)},
        )
    if code in _RETRYABLE_HTTP_STATUS_CODES or code >= 500:
        return ProviderTransientError(f"HTTP {exc.code}: {detail}")
    return RuntimeError(f"HTTP {exc.code}: {detail}")


def _runtime_decode_error(exc: BaseException, request: GatewayRequest) -> ProviderResponseError:
    """Normalize undecodable / non-JSON provider response bodies into a recoverable error.

    坏字节(非 UTF-8)或非 JSON 响应体不是任务本身的 bug:归一为 ProviderResponseError
    (可恢复),携带 error_code 便于上游识别,而不是让 UnicodeDecodeError/JSONDecodeError
    裸奔崩掉整轮。"""
    return ProviderResponseError(
        "模型接口返回了无法解码或非 JSON 的响应体: "
        f"url={request.url} 底层错误: {type(exc).__name__}: {exc}",
        error_code="MODEL_RESPONSE_NOT_DECODABLE",
    )


def _runtime_network_error(exc: BaseException, request: GatewayRequest) -> RuntimeError:
    """Classify network exceptions into timeout, transient provider flake, or config failure."""
    parsed = urllib.parse.urlparse(request.url)
    host = parsed.netloc or parsed.path.split("/", 1)[0] or request.api_base
    reason = _network_error_text(exc)
    if _is_timeout_exception(exc):
        return ProviderTimeoutError(
            "模型接口请求超时: "
            f"host={host} request_timeout={request.timeout}s url={request.url} "
            f"底层错误: {reason}"
        )
    if _is_transient_network_error(exc):
        return ProviderTransientError(
            "网络请求失败: "
            f"模型接口 {host} 临时断开或连接被重置（{request.url}）。"
            "本次请求可由重试/恢复/接管继续处理；"
            f"底层错误: {reason}"
        )
    return RuntimeError(
        "网络请求失败: "
        f"无法连接模型接口 {host}（{request.url}）。"
        "请检查 DNS、网络/代理和 api_base 配置；"
        f"底层错误: {reason}"
    )


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


def _is_transient_network_error(exc: BaseException) -> bool:
    if _is_timeout_exception(exc):
        return False
    text = _network_error_text(exc).lower()
    return any(marker in text for marker in _RETRYABLE_NETWORK_ERROR_MARKERS)


def _network_error_text(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    parts = [
        exc.__class__.__name__,
        str(reason or ""),
        reason.__class__.__name__ if reason is not None else "",
        str(exc),
    ]
    return " ".join(part for part in parts if part).strip() or exc.__class__.__name__


def _provider_error_indicates_context_window(detail: str) -> bool:
    payload = _provider_error_payload(detail)
    structured_text = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else ""
    text = f"{structured_text} {detail or ''}".lower()
    return any(marker in text for marker in _CONTEXT_WINDOW_ERROR_MARKERS)


def _provider_error_payload(detail: str) -> object:
    try:
        return json.loads(detail)
    except (TypeError, ValueError):
        return {}


def _stream_deadline(timeout: int) -> float:
    """Convert request timeout seconds into a monotonic streaming deadline."""
    return time.monotonic() + max(1, int(timeout or 0))


def _iter_sse_data_lines(response, *, deadline: float, timeout: int, url: str) -> Iterator[str]:
    for raw_line in response:
        if time.monotonic() > deadline:
            raise ProviderTimeoutError(
                "模型接口流式响应超时: "
                f"request_timeout={timeout}s url={url}"
            )
        # provider 流里可能混入坏字节/非 UTF-8 切片(分块边界把多字节字符截断),
        # 用 errors="replace" 兜底,不让单行解码异常崩掉整条流式响应。
        line = raw_line.decode("utf-8", "replace").strip()
        if _is_sse_data_line(line):
            yield line[5:].strip()


def _is_sse_data_line(line: str) -> bool:
    """Return True for SSE data lines; the caller handles terminal markers."""
    return bool(line and line.startswith("data:"))
