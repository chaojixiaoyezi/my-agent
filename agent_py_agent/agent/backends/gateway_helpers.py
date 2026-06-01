# LLM: Model backend module; keep streaming, gateway, and backend protocol shapes stable.
# 模块用途: 封装模型后端协议、流式解析和 gateway 辅助调用。

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

from .errors import ProviderTimeoutError, ProviderTransientError

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


# LLM: GatewayRequest is the immutable provider-call boundary object.
# 类用途: 保存一次 gateway HTTP 调用所需的 URL、鉴权、payload、header 和超时配置。
@dataclass(frozen=True)
class GatewayRequest:
    """Immutable request envelope for one provider HTTP call."""

    api_base: str
    api_key: str
    path: str
    payload: dict[str, Any]
    headers: dict[str, str]
    timeout: int

    # LLM: url is derived from the validated base and path instead of being supplied separately.
    # 函数用途: 拼出最终 provider endpoint，避免调用方同时传 base/path/url 三套入口。
    @property
    def url(self) -> str:
        """Return the final endpoint after joining provider base URL and API path."""
        return self.api_base + self.path


# LLM: post_json is the non-streaming gateway entry used by provider adapters.
# 函数用途: 发送 JSON 请求并把 provider/http/network 错误归一成可恢复或普通运行时错误。
def post_json(
    request: GatewayRequest,
) -> dict[str, Any]:
    """POST JSON and normalize provider/network failures into typed exceptions."""
    _require_api_key(request.api_key)
    try:
        with _open_gateway_request(request) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _runtime_network_error(exc, request) from exc


# LLM: post_stream is the list-returning streaming helper for callers that need buffered output.
# 函数用途: 发起流式请求并收集 SSE data 行，复用同一套错误分类和超时逻辑。
def post_stream(
    request: GatewayRequest,
) -> list[str]:
    """POST a streaming request and collect SSE data lines."""
    return list(_post_stream_lines(request))


# LLM: post_stream_iter is the iterator streaming helper for incremental model output.
# 函数用途: 发起流式请求并逐条产出 SSE data 行，供 CLI/TUI 边读边显示。
def post_stream_iter(
    request: GatewayRequest,
):
    """POST a streaming request and yield normalized SSE data lines."""
    yield from _post_stream_lines(request)


# LLM: _post_stream_lines is the single implementation behind buffered and iterator streaming.
# 函数用途: 强制打开 stream 模式、设置总时限，并统一处理 HTTP/network/provider 错误。
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


# LLM: _urllib_request keeps gateway transport structured and avoids shell/string execution paths.
# 函数用途: 将 GatewayRequest 转成 urllib Request，集中编码 JSON 和 headers。
def _urllib_request(request: GatewayRequest) -> urllib.request.Request:
    """Build the urllib request without exposing shell/string transport paths."""
    return urllib.request.Request(
        request.url,
        data=json.dumps(request.payload).encode("utf-8"),
        method="POST",
        headers=request.headers,
    )


# LLM: _open_gateway_request retries pre-response transient provider overloads and network disconnects.
# 函数用途: 打开模型 HTTP 请求；对 429/529/503 和 EOF/远端断开等临时错误做短暂重试，非临时错误仍原样抛出。
def _open_gateway_request(request: GatewayRequest):
    last_attempt = len(_RETRYABLE_HTTP_DELAYS_SECONDS)
    for attempt in range(last_attempt + 1):
        response = _gateway_request_attempt(request, attempt, last_attempt)
        if response is not None:
            return response
    raise RuntimeError("unreachable gateway retry state")


# LLM: _gateway_request_attempt wraps one provider call and sleeps only for retryable pre-response errors.
# 函数用途: 执行一次 gateway HTTP 请求；可重试 HTTP/网络临时错误返回 None，非可重试错误原样抛出。
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


# LLM: _should_retry_http_error keeps auth/schema errors fail-fast while retrying provider overload and gateway flakiness.
# 函数用途: 判断 HTTP 错误是否属于可重试的临时服务端/限流错误。
def _should_retry_http_error(exc: urllib.error.HTTPError, attempt: int, last_attempt: int) -> bool:
    return attempt < last_attempt and int(getattr(exc, "code", 0) or 0) in _RETRYABLE_HTTP_STATUS_CODES


# LLM: _should_retry_network_error retries only provider-side disconnect shapes, never local timeout/auth/config failures.
# 函数用途: 判断网络异常是否属于可短暂重试的模型服务临时断连；超时和 DNS/配置错误不在这里重试。
def _should_retry_network_error(exc: BaseException, attempt: int, last_attempt: int) -> bool:
    return attempt < last_attempt and _is_transient_network_error(exc)


# LLM: _retry_delay_seconds honors Retry-After when providers send it, with a cap to avoid unbounded CLI stalls.
# 函数用途: 计算 HTTP 临时失败后的等待秒数；没有 Retry-After 时使用短阶梯退避。
def _retry_delay_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    header_delay = _retry_after_header_seconds(exc)
    if header_delay is not None:
        return min(header_delay, _MAX_RETRY_AFTER_SECONDS)
    index = min(attempt, len(_RETRYABLE_HTTP_DELAYS_SECONDS) - 1)
    return _RETRYABLE_HTTP_DELAYS_SECONDS[index]


# LLM: _network_retry_delay_seconds uses the same bounded retry cadence for provider disconnects.
# 函数用途: 计算临时网络断连后的等待秒数；不读取 header，只用短阶梯退避。
def _network_retry_delay_seconds(attempt: int) -> float:
    index = min(attempt, len(_RETRYABLE_HTTP_DELAYS_SECONDS) - 1)
    return _RETRYABLE_HTTP_DELAYS_SECONDS[index]


# LLM: _retry_after_header_seconds parses provider retry hints without depending on a specific HTTP header object type.
# 函数用途: 从 Retry-After 头读取秒数；无法解析时返回 None 让默认退避接管。
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


# LLM: _require_api_key fails before network IO so missing credentials are never retried as provider flake.
# 函数用途: 在发起 HTTP 请求前校验 API key 是否存在，并给出明确配置错误。
def _require_api_key(api_key: str) -> None:
    """Fail fast for missing credentials before opening a network connection."""
    if not api_key:
        raise ValueError("api_key 为空：请在配置文件中填写 API Key。")


# LLM: _runtime_http_error is the authoritative HTTP status classifier for provider calls.
# 函数用途: 把 5xx/429 等临时错误归为可恢复 provider 错误，其余保留为普通运行时错误。
def _runtime_http_error(exc: urllib.error.HTTPError) -> RuntimeError:
    """Classify provider HTTP errors at the backend boundary."""
    detail = exc.read().decode("utf-8", "replace")
    code = int(getattr(exc, "code", 0) or 0)
    if code in _RETRYABLE_HTTP_STATUS_CODES or code >= 500:
        return ProviderTransientError(f"HTTP {exc.code}: {detail}")
    return RuntimeError(f"HTTP {exc.code}: {detail}")


# LLM: _runtime_network_error is the authoritative network exception classifier for provider calls.
# 函数用途: 区分 timeout、临时断连和配置/DNS 类失败，并生成可读错误信息。
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


# LLM: _is_timeout_exception recognizes urllib/socket timeout shapes without broad message matching.
# 函数用途: 把直接 TimeoutError 和 URLError.reason 里的 timeout 都归一成 provider timeout。
def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


# LLM: _is_transient_network_error keeps retry classification structured instead of matching parent prompts or runner text.
# 函数用途: 识别 provider 临时断连类网络异常；只看异常类型和底层错误文本，避免把 DNS/配置错误误判为可重试。
def _is_transient_network_error(exc: BaseException) -> bool:
    if _is_timeout_exception(exc):
        return False
    text = _network_error_text(exc).lower()
    return any(marker in text for marker in _RETRYABLE_NETWORK_ERROR_MARKERS)


# LLM: _network_error_text flattens urllib reason chains so retry and messages classify the same evidence.
# 函数用途: 把 URLError.reason、异常文本和异常类名拼成稳定文本，供重试判断和用户提示复用。
def _network_error_text(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    parts = [
        exc.__class__.__name__,
        str(reason or ""),
        reason.__class__.__name__ if reason is not None else "",
        str(exc),
    ]
    return " ".join(part for part in parts if part).strip() or exc.__class__.__name__


# LLM: _stream_deadline turns request timeout into an absolute wall-clock bound for streaming.
# 函数用途: 计算流式响应的总截止时间，防止 socket 一直有心跳但任务永不结束。
def _stream_deadline(timeout: int) -> float:
    """Convert request timeout seconds into a monotonic streaming deadline."""
    return time.monotonic() + max(1, int(timeout or 0))


# LLM: _iter_sse_data_lines enforces total stream wall-clock timeout, not only socket idle timeout.
# 函数用途: 迭代 SSE 数据行；如果服务端持续发空心跳但没有结束，也会按 request_timeout 总时长退出。
def _iter_sse_data_lines(response, *, deadline: float, timeout: int, url: str) -> Iterator[str]:
    for raw_line in response:
        if time.monotonic() > deadline:
            raise ProviderTimeoutError(
                "模型接口流式响应超时: "
                f"request_timeout={timeout}s url={url}"
            )
        line = raw_line.decode("utf-8").strip()
        if _is_sse_data_line(line):
            yield line[5:].strip()


# LLM: _is_sse_data_line keeps SSE parsing narrow to data lines only.
# 函数用途: 判断一行是否为可交给上层解析的 SSE data 行，注释/空行直接跳过。
def _is_sse_data_line(line: str) -> bool:
    """Return True for SSE data lines; the caller handles terminal markers."""
    return bool(line and line.startswith("data:"))
