
# LLM: Provider HTTP transport normalizes retries, timeouts, response decoding, and typed user interrupts; keep it independent from backend/runtime initialization cycles.
# 模块用途: 统一发送模型 HTTP 请求，并在超时、网络异常或用户停止时及时收回连接。
from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .errors import (
    ProviderContextWindowError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
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
        "available context size",
        "context size",
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
    connect_timeout: float = 10.0

    @property
    def url(self) -> str:
        """Return the final endpoint after joining provider base URL and API path."""
        return self.api_base + self.path


# LLM: Non-streaming POST reads register the current task's abort hook and must preserve typed provider errors for callers.
# 函数用途: 发送非流式 JSON 请求；用户停止时主动关闭响应，不等完整请求超时。
def post_json(
    request: GatewayRequest,
) -> dict[str, Any]:
    """POST JSON and normalize provider/network failures into typed exceptions."""
    _require_api_key(request.api_key)
    try:
        with _open_gateway_request(request) as resp:
            with _provider_interrupt_callback(lambda: _abort_stream_response(resp)):
                raw = resp.read()
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口请求已被用户停止") from exc
        raise _runtime_network_error(exc, request) from exc
    # decode/loads 在 with 外做:坏字节(非 UTF-8)或非 JSON 响应体不能漏出去崩整轮,
    # 归一为可恢复的 ProviderResponseError(适配器无法解析,不是任务本身的 bug)。
    try:
        text = raw.decode("utf-8", "replace")
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _runtime_decode_error(exc, request) from exc


# LLM: Metadata GET follows the same interrupt contract as inference calls without turning discovery failures into model output.
# 函数用途: 读取模型元数据；若当前任务被停止，立即中断正在等待的响应。
def get_json(request: GatewayRequest) -> dict[str, Any]:
    """GET provider metadata without turning discovery failure into a model run failure."""
    _require_api_key(request.api_key)
    req = urllib.request.Request(request.url, method="GET", headers=request.headers)
    try:
        with _gateway_urlopen(req, request) as resp:
            with _provider_interrupt_callback(lambda: _abort_stream_response(resp)):
                raw = resp.read()
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口请求已被用户停止") from exc
        raise _runtime_network_error(exc, request) from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
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


# LLM: Streaming callers share this typed error boundary; user interrupts must never be retried or mislabeled as provider failures.
# 函数用途: 统一启动 SSE 流并归一异常，将用户停止保留为独立中断事件。
def _post_stream_lines(request: GatewayRequest) -> Iterator[str]:
    """Shared streaming implementation used by list and iterator callers."""
    request.payload["stream"] = True
    _require_api_key(request.api_key)
    deadline = _stream_deadline(request.timeout)
    try:
        yield from _stream_with_watchdog(request, deadline)
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口流式请求已被用户停止") from exc
        raise _runtime_network_error(exc, request) from exc


# LLM: One response close hook serves both deadline enforcement and typed task interruption; always cancel the timer on exit.
# 函数用途: 读取流式模型响应，超时或用户停止时主动关闭 socket 解除阻塞。
def _stream_with_watchdog(request: GatewayRequest, deadline: float) -> Iterator[str]:
    # 看门狗(硬超时兜底):SSE 流若因 provider 中途 trickle 字节但不完成整行,readline 会永久阻塞
    #   在半行上,而 deadline 检查在逐行循环体内、永远执行不到→request_timeout 形同虚设、子代理冻结
    #   (实测万行任务多个子代理冻结 30 分钟无任何超时/重试日志=正是此洞)。定时器到点强制关 socket
    #   解除 readline 阻塞,让 request_timeout 真正生效;正常读完即 cancel、零副作用。
    with _open_gateway_request(request) as resp:
        with _provider_interrupt_callback(lambda: _abort_stream_response(resp)):
            if _provider_is_interrupted():
                raise InterruptedError("模型接口流式请求已被用户停止")
            watchdog = threading.Timer(max(1, int(request.timeout or 0)), _abort_stream_response, args=(resp,))
            watchdog.daemon = True
            watchdog.start()
            try:
                yield from _iter_sse_data_lines(resp, deadline=deadline, timeout=request.timeout, url=request.url)
            finally:
                watchdog.cancel()


def _abort_stream_response(resp: Any) -> None:
    """看门狗到点强制关闭流式响应,解除 readline 在"半行 trickle"上的永久阻塞(硬超时兜底)。"""
    try:
        resp.close()
    except Exception:
        pass


# LLM: Keep the backend module importable while runtime_errors is initializing; resolve the higher-level interruption registry only when a request is active.
# 函数用途: 在真正发模型请求时才挂停止回调，避免后端与并发包在模块导入阶段互相引用。
@contextmanager
def _provider_interrupt_callback(callback):
    from ..concurrency.interrupt import register_interrupt_callback

    with register_interrupt_callback(callback):
        yield


# LLM: Provider transport reads the current execution thread's typed interruption state through a lazy dependency boundary.
# 函数用途: 判断当前模型请求是否已收到用户停止信号，同时保持后端模块没有初始化环。
def _provider_is_interrupted() -> bool:
    from ..concurrency.interrupt import is_interrupted

    return is_interrupted()


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
        return _gateway_urlopen(req, request)
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


def _gateway_urlopen(req: urllib.request.Request, request: GatewayRequest):
    """Open one provider request with distinct connect and read timeouts.

    ``urllib`` normally applies one timeout to TCP connect, proxy tunnel, TLS,
    response headers and body reads.  Long-reasoning models need a generous
    read window, but using that same value for connect made a stale endpoint
    consume the full 600 seconds.  The custom connections keep standard proxy
    handlers and TLS behavior, then switch the live socket to the long read
    timeout immediately after connect completes.
    """

    connect_timeout = _bounded_connect_timeout(request)
    read_timeout = max(1.0, float(request.timeout or 0))
    opener = urllib.request.build_opener(
        _SplitTimeoutHTTPHandler(connect_timeout, read_timeout),
        _SplitTimeoutHTTPSHandler(connect_timeout, read_timeout),
    )
    return opener.open(req, timeout=read_timeout)


def _bounded_connect_timeout(request: GatewayRequest) -> float:
    read_timeout = max(1.0, float(request.timeout or 0))
    configured = max(0.2, float(request.connect_timeout or 0))
    return min(configured, read_timeout)


class _SplitTimeoutHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host: str,
        port: int | None = None,
        timeout: object | None = None,
        source_address: tuple[str, int] | None = None,
        blocksize: int = 8192,
        *,
        connect_timeout: float,
        read_timeout: float,
    ):
        del timeout
        self._provider_read_timeout = read_timeout
        super().__init__(
            host,
            port=port,
            timeout=connect_timeout,
            source_address=source_address,
            blocksize=blocksize,
        )

    def connect(self) -> None:
        super().connect()
        if self.sock is not None:
            self.sock.settimeout(self._provider_read_timeout)


class _SplitTimeoutHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: object | None = None,
        source_address: tuple[str, int] | None = None,
        context: ssl.SSLContext | None = None,
        blocksize: int = 8192,
        connect_timeout: float,
        read_timeout: float,
    ):
        del timeout
        self._provider_read_timeout = read_timeout
        super().__init__(
            host,
            port=port,
            timeout=connect_timeout,
            source_address=source_address,
            context=context,
            blocksize=blocksize,
        )

    def connect(self) -> None:
        super().connect()
        if self.sock is not None:
            self.sock.settimeout(self._provider_read_timeout)


class _SplitTimeoutHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, connect_timeout: float, read_timeout: float):
        super().__init__()
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def http_open(self, req):
        return self.do_open(
            _SplitTimeoutHTTPConnection,
            req,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
        )


class _SplitTimeoutHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, connect_timeout: float, read_timeout: float):
        super().__init__()
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def https_open(self, req):
        return self.do_open(
            _SplitTimeoutHTTPSConnection,
            req,
            context=self._context,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
        )
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
    if code == 429:
        return ProviderUsageLimitError(f"HTTP {exc.code}: {detail}")
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
            f"host={host} connect_timeout={_bounded_connect_timeout(request):g}s "
            f"request_timeout={request.timeout}s url={request.url} "
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


# LLM: Check the current execution's interrupt flag at every SSE boundary before exposing provider data to higher layers.
# 函数用途: 逐行读取 SSE 数据，并在每个安全点检查超时与用户停止。
def _iter_sse_data_lines(response, *, deadline: float, timeout: int, url: str) -> Iterator[str]:
    for raw_line in response:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口流式请求已被用户停止")
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
