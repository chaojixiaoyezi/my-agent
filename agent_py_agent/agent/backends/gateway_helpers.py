
# LLM: Provider HTTP transport normalizes retries, timeouts, response decoding, and typed user interrupts; keep it independent from backend/runtime initialization cycles.
# 模块用途: 统一发送模型 HTTP 请求，并在超时、网络异常或用户停止时及时收回连接。
from __future__ import annotations

import http.client
import ipaddress
import json
import re
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
    ProviderQuotaExhaustedError,
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
_HARD_QUOTA_ERROR_CODES = frozenset(
    {
        "billing_hard_limit_reached",
        "credits_depleted",
        "insufficient_quota",
        "quota_exceeded",
        "token_plan_exhausted",
        "usage_limit_reached",
        "usage_not_included",
    }
)
_PROVIDER_ERROR_CODE_KEYS = frozenset({"code", "error_code", "reason", "status", "type"})
_HTTP_ERROR_DETAIL_ATTR = "_my_agent_provider_error_detail"
_PROVIDER_ATTEMPT_OBSERVER = threading.local()


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


@contextmanager
def provider_attempt_observer(callback):
    """Observe physical inference HTTP attempts in the current provider thread."""

    previous = getattr(_PROVIDER_ATTEMPT_OBSERVER, "callback", None)
    _PROVIDER_ATTEMPT_OBSERVER.callback = callback
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_PROVIDER_ATTEMPT_OBSERVER, "callback")
            except AttributeError:
                pass
        else:
            _PROVIDER_ATTEMPT_OBSERVER.callback = previous


def _emit_provider_attempt(event: dict[str, object]) -> None:
    callback = getattr(_PROVIDER_ATTEMPT_OBSERVER, "callback", None)
    if not callable(callback):
        return
    try:
        callback(dict(event))
    except Exception:
        # Observability must never alter the provider request outcome.
        return


# LLM: Non-streaming POST reads register the current task's abort hook and must preserve typed provider errors for callers.
# 函数用途: 发送非流式 JSON 请求；用户停止时主动关闭响应，不等完整请求超时。
def post_json(
    request: GatewayRequest,
) -> dict[str, Any]:
    """POST JSON and normalize provider/network failures into typed exceptions."""
    _require_api_key(request.api_key)
    try:
        with _gateway_response_scope(_open_gateway_request(request)) as (resp, response_guard):
            with _provider_interrupt_callback(response_guard.abort):
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
        with _gateway_response_scope(_gateway_urlopen(req, request)) as (resp, response_guard):
            with _provider_interrupt_callback(response_guard.abort):
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
    try:
        yield from _stream_with_watchdog(request)
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口流式请求已被用户停止") from exc
        raise _runtime_network_error(exc, request) from exc


# LLM: Match 会话运行时's stream idle boundary: valid SSE data resets the deadline, while comments, half-lines, and silence do not.
# 函数用途: 读取流式模型响应，空闲超时或用户停止时主动关闭 socket 解除阻塞。
def _stream_with_watchdog(request: GatewayRequest) -> Iterator[str]:
    with _gateway_response_scope(_open_gateway_request(request)) as (resp, response_guard):
        with _provider_interrupt_callback(response_guard.abort):
            if _provider_is_interrupted():
                raise InterruptedError("模型接口流式请求已被用户停止")
            # 门槛3补证(steward seq1533-1): idle/watchdog/wall 共用同一 monotonic
            # cutoff——start 一次起算, wall_deadline 显式传入, idle 初始与其同基准,
            # 不再「watchdog 与 _iter_sse_data_lines 分别起算时间」。
            # 门槛2 终审边界①(seq1622-1): watchdog 构造不再独立调用
            # time.monotonic(), 初始 idle deadline 由调用方传入同一 start 派生
            # 的 wall_deadline——watchdog/parser 全链路同一 cutoff 起点。
            start = time.monotonic()
            wall_deadline = start + _stream_deadline_offset(request.timeout)
            watchdog = _StreamIdleWatchdog(
                response_guard, request.timeout, idle_deadline=wall_deadline
            )
            watchdog.start()
            try:
                yield from _iter_sse_data_lines(
                    resp,
                    timeout=request.timeout,
                    url=request.url,
                    on_data_line=watchdog.touch,
                    wall_deadline=wall_deadline,
                )
                if watchdog.timed_out:
                    raise _stream_idle_timeout_error(request)
            except (OSError, ValueError) as exc:
                if watchdog.timed_out:
                    raise _stream_idle_timeout_error(request) from exc
                raise
            except AttributeError as exc:
                # ``HTTPResponse.close()`` may clear the buffered reader while
                # the owning thread is still unwinding ``readline()``.  That
                # reader then raises ``AttributeError`` even though the actual
                # event was our typed idle timeout or user interruption.
                # Reclassify only when the structured cancellation state proves
                # teardown caused it; a genuine provider/parser AttributeError
                # must remain visible as a programmer bug.
                if watchdog.timed_out:
                    raise _stream_idle_timeout_error(request) from exc
                if _provider_is_interrupted():
                    raise InterruptedError("模型接口流式请求已被用户停止") from exc
                raise
            finally:
                watchdog.cancel()


class _StreamIdleWatchdog:
    """Close one blocked response only after a full idle interval.

    门槛2 终审边界①(seq1622-1): idle deadline 模型——初始 deadline 由调用方
    传入(与 parser 的 wall_deadline 同一 start 派生值), data 触达后按与
    parser 同一 offset 公式(`_stream_deadline_offset`)滚动。watchdog 不再
    构造时独立调用 time.monotonic() 起算, 与 parser 全链路同一 cutoff 起点;
    <1s 预算 floor 也由同一 offset 合同承载(parser 侧同 floor, 无分叉)。
    """

    def __init__(
        self,
        response_guard: _GatewayResponseGuard,
        timeout: int | float,
        *,
        idle_deadline: float,
    ) -> None:
        self._response_guard = response_guard
        self._timeout = _stream_deadline_offset(timeout)
        self._idle_deadline = idle_deadline
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._cancelled = False
        self._timed_out = False

    def start(self) -> None:
        with self._lock:
            self._schedule_locked(self._idle_deadline - time.monotonic())

    def touch(self) -> None:
        with self._lock:
            if not self._cancelled:
                # 与 parser 的 idle_deadline 重置同一公式: now + offset
                self._idle_deadline = time.monotonic() + self._timeout

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    @property
    def timed_out(self) -> bool:
        with self._lock:
            return self._timed_out

    def _check(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            remaining = self._idle_deadline - time.monotonic()
            if remaining > 0:
                self._schedule_locked(remaining)
                return
            self._cancelled = True
            self._timed_out = True
            self._timer = None
        self._response_guard.abort()

    def _schedule_locked(self, delay: float) -> None:
        timer = threading.Timer(max(0.001, delay), self._check)
        timer.daemon = True
        self._timer = timer
        timer.start()


def _stream_idle_timeout_error(request: GatewayRequest) -> ProviderTimeoutError:
    return ProviderTimeoutError(
        "模型接口流式响应空闲超时: "
        f"request_timeout={request.timeout}s url={request.url}",
        stage="stream_idle",
    )


class _GatewayResponseGuard:
    """Serialize normal close, user cancellation, and idle-timeout teardown.

    CPython's ``HTTPResponse.close()`` checks ``self.fp`` and clears it in two
    separate operations.  Two concurrent close callers can both pass the
    check, after which one observes ``fp=None`` and raises
    ``AttributeError``.  Keep one response-owned lock around every close path
    instead of teaching higher layers to reinterpret that race.
    """

    def __init__(self, response: Any) -> None:
        self.response = response
        self._lock = threading.Lock()
        self._closed = False

    def abort(self) -> None:
        """Cancel a blocked read and close exactly once without surfacing cleanup noise."""
        with self._lock:
            if self._closed:
                return
            transport = _stdlib_response_socket(self.response)
            if transport is not None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass
            self._close_locked(suppress_errors=True)

    def close(self) -> None:
        """Close exactly once on the owning request thread."""
        with self._lock:
            self._close_locked(suppress_errors=False)

    def _close_locked(self, *, suppress_errors: bool) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.response.close()
        except Exception:
            if not suppress_errors:
                raise


@contextmanager
def _gateway_response_scope(response: Any):
    """Own one provider response without invoking urllib's racy context exit."""
    guard = _GatewayResponseGuard(response)
    try:
        yield response, guard
    finally:
        guard.close()


def _stdlib_response_socket(resp: Any) -> socket.socket | None:
    """Return CPython urllib's one transport socket without probing arbitrary objects."""
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    transport = getattr(raw, "_sock", None)
    return transport if isinstance(transport, socket.socket) else None


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
    headers = dict(request.headers or {})
    # Cloudflare 1010 拦截根因(真机实测):urllib 默认无 User-Agent 时,工具运行时.ai 等
    # Cloudflare 端点对无 UA 的脚本请求返回 403 error code:1010;带浏览器 UA 则 200。
    # 统一补 UA,兼容所有 Cloudflare 防护的 Anthropic 兼容端点。
    headers.setdefault("User-Agent", "my-agent/1.0 (anthropic-compatible client)")
    return urllib.request.Request(
        request.url,
        data=json.dumps(request.payload).encode("utf-8"),
        method="POST",
        headers=headers,
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
    attempt_id = f"provider-http:{time.time_ns()}:{attempt + 1}"
    base_event = {
        "attempt_id": attempt_id,
        "method": "POST",
        "path": request.path,
    }
    _emit_provider_attempt({**base_event, "status": "started"})
    try:
        response = _gateway_urlopen(req, request)
    except urllib.error.HTTPError as exc:
        retry_scheduled = _should_retry_http_error(exc, attempt, last_attempt)
        _emit_provider_attempt(
            {
                **base_event,
                "status": "failed",
                "http_status": int(getattr(exc, "code", 0) or 0),
                "error_type": type(exc).__name__,
                "retry_scheduled": retry_scheduled,
            }
        )
        if not retry_scheduled:
            raise
        _provider_retry_wait(_retry_delay_seconds(exc, attempt))
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        retry_scheduled = _should_retry_network_error(exc, attempt, last_attempt)
        _emit_provider_attempt(
            {
                **base_event,
                "status": "failed",
                "error_type": type(exc).__name__,
                "retry_scheduled": retry_scheduled,
            }
        )
        if not retry_scheduled:
            raise
        _provider_retry_wait(_network_retry_delay_seconds(attempt))
        return None
    _emit_provider_attempt(
        {
            **base_event,
            "status": "response_opened",
            "http_status": int(getattr(response, "status", 0) or 0),
        }
    )
    return response


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
        _provider_proxy_handler(req.full_url),
        _SplitTimeoutHTTPHandler(connect_timeout, read_timeout),
        _SplitTimeoutHTTPSHandler(connect_timeout, read_timeout),
    )
    return opener.open(req, timeout=read_timeout)


def _provider_proxy_handler(url: str) -> urllib.request.ProxyHandler:
    """Keep local model/Gateway traffic off ambient desktop proxies.

    External providers retain urllib's configured proxy behavior.  Explicit
    loopback hosts are always local process boundaries; sending them through a
    system proxy breaks local fallback and can expose local request metadata.
    """

    host = str(urllib.parse.urlsplit(url).hostname or "").strip().lower().rstrip(".")
    if _is_loopback_host(host):
        return urllib.request.ProxyHandler({})
    return urllib.request.ProxyHandler()


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


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
            # 2026-08-16 3×3 真机卡死根因(4 例, py-spy 线程栈实锤):
            # Python 3.11 的 wrap_socket 是**惰性握手**——TLS 握手在首次
            # send 时执行。旧代码在 connect() 里 wrap 后立刻
            # settimeout(read_timeout), 握手因此用了长读超时(240-600s)
            # 而非 connect_timeout(10s)——工具运行时 端点 TLS 无响应时
            # 永久挂起(主线程 queue.get 等结果, guard 线程卡在握手)。
            # 修复: 显式先完成握手(仍用 connect_timeout), 再切换长读超时。
            # do_handshake 幂等(已握手则 no-op); 握手失败异常传播给上层
            # 重试(与 TCP connect 失败同路径)。
            self.sock.do_handshake()
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
    code = int(getattr(exc, "code", 0) or 0)
    if code == 429 and _provider_error_indicates_quota_exhausted(_http_error_detail(exc)):
        return False
    if _is_silent_bad_request(exc):
        return attempt < last_attempt
    return attempt < last_attempt and code in _RETRYABLE_HTTP_STATUS_CODES


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


def _provider_retry_wait(delay_seconds: float) -> None:
    """Wait between transport retries while preserving the current task's /stop token."""
    from ..concurrency.interrupt import wait_interruptibly

    wait_interruptibly(delay_seconds)


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
    detail = _http_error_detail(exc)
    code = int(getattr(exc, "code", 0) or 0)
    if code in _CONTEXT_WINDOW_HTTP_STATUS_CODES and _provider_error_indicates_context_window(detail):
        return ProviderContextWindowError(
            f"HTTP {exc.code}: {detail}",
            details={"status_code": code, "provider_error": _provider_error_payload(detail)},
        )
    if code == 429:
        payload = _provider_error_payload(detail)
        if _provider_error_indicates_quota_exhausted(detail):
            return ProviderQuotaExhaustedError(
                f"HTTP {exc.code}: {detail}",
                details={"status_code": code, "provider_error": payload},
            )
        return ProviderUsageLimitError(f"HTTP {exc.code}: {detail}")
    if code in _RETRYABLE_HTTP_STATUS_CODES or code >= 500:
        return ProviderTransientError(f"HTTP {exc.code}: {detail}")
    if _is_silent_bad_request(exc):
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
            f"底层错误: {reason}",
            stage="provider_declared",
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


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    cached = getattr(exc, _HTTP_ERROR_DETAIL_ATTR, None)
    if isinstance(cached, str):
        return cached
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    if isinstance(raw, bytes):
        detail = raw.decode("utf-8", "replace")
    else:
        detail = str(raw or "")
    setattr(exc, _HTTP_ERROR_DETAIL_ATTR, detail)
    return detail


def _provider_error_indicates_quota_exhausted(detail: str) -> bool:
    """Recognize provider-declared hard quota using error payload facts, not user text."""
    payload = _provider_error_payload(detail)
    codes = _provider_error_codes(payload)
    if codes & _HARD_QUOTA_ERROR_CODES:
        return True
    normalized = str(detail or "").lower().replace("-", "_").replace(" ", "_")
    if any(code in normalized for code in _HARD_QUOTA_ERROR_CODES):
        return True
    if re.search(r"(?:^|\D)2056(?:\D|$)", str(detail or "")):
        return True
    return "token_plan" in normalized and any(
        marker in normalized for marker in ("用量上限", "购买积分", "upgrade", "exhausted")
    )


def _provider_error_codes(payload: object) -> set[str]:
    values: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in _PROVIDER_ERROR_CODE_KEYS and isinstance(value, str | int):
                values.add(str(value).strip().lower().replace("-", "_").replace(" ", "_"))
            if isinstance(value, dict | list):
                values.update(_provider_error_codes(value))
    elif isinstance(payload, list):
        for value in payload:
            values.update(_provider_error_codes(value))
    return values


def _provider_error_payload(detail: str) -> object:
    try:
        return json.loads(detail)
    except (TypeError, ValueError):
        return {}


def _is_silent_bad_request(exc: urllib.error.HTTPError) -> bool:
    """A 400 whose body carries no error/message explanation (echo-only or empty).

    聚合网关(如 工具运行时)偶发对瞬时拒绝回 400,且响应体只有请求字段回显(如
    {"model": ...})或空,没有 error/message 说明拒绝原因——同样的请求稍后即成功。
    这类"哑 400"不是请求本身无效:按可恢复瞬时错误进重试链,而不是当程序 bug
    一票否决整个任务(真机实锤:记住任务被哑 400 永久放弃,记忆没写入,后续
    召回全错)。带明确错误说明(error/message 字段)的 400 保持永久失败语义。
    判据只用结构化字段存在性,不做文本匹配。
    """
    code = int(getattr(exc, "code", 0) or 0)
    if code != 400:
        return False
    detail = _http_error_detail(exc)
    if not detail.strip():
        return True
    payload = _provider_error_payload(detail)
    if not isinstance(payload, dict):
        return True
    return "error" not in payload and "message" not in payload


def _stream_deadline_offset(timeout: int | float) -> float:
    """预算秒数(浮点, 不截断): 最小 1.0s 防 0 预算死循环, 与门槛1/2 账本
    effective 值一致——dynamic 预算可带小数(如 303.1), int() 截断会放大
    0.05/0.5s 级预算(steward seq1533-2)。"""
    return max(1.0, float(timeout or 0))


def _stream_deadline(timeout: int | float) -> float:
    """Convert request timeout seconds into the next monotonic idle deadline."""
    return time.monotonic() + _stream_deadline_offset(timeout)


# LLM: Check the current execution's interrupt flag at every SSE boundary before exposing provider data to higher layers.
# 函数用途: 逐行读取 SSE 数据，并在每个安全点检查超时与用户停止。
def _iter_sse_data_lines(
    response,
    *,
    timeout: int,
    url: str,
    on_data_line,
    wall_deadline: float | None = None,
) -> Iterator[str]:
    # 门槛3补证: 显式 wall_deadline 时 idle 初始与其共用同一 cutoff(同基准,
    # 由 _stream_with_watchdog 一次 start 起算); 未显式时按进入时点兜底。
    if wall_deadline is None:
        wall_deadline = time.monotonic() + _stream_deadline_offset(timeout)
    idle_deadline = wall_deadline
    # 门槛3: 总墙钟硬顶(=effective 值)。data 行只重置 idle_deadline 不动
    # wall_deadline——持续 data 续命下, 墙钟预算是唯一能掐断流的总时长硬顶
    # (修掉「有 data 就永远等」的窗口)。
    for raw_line in response:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口流式请求已被用户停止")
        # idle 检查在前保持门槛2 语义(保活空行=stream_idle), wall 在后只拦
        # 「data 持续续命超总预算」——同刻到期时 idle 赢, 不回归既有锁定。
        if time.monotonic() > idle_deadline:
            raise ProviderTimeoutError(
                "模型接口流式响应空闲超时: "
                f"request_timeout={timeout}s url={url}",
                stage="stream_idle",
            )
        if time.monotonic() > wall_deadline:
            raise ProviderTimeoutError(
                "模型接口流式响应总墙钟超时: "
                f"request_timeout={timeout}s url={url}",
                # 门槛3: transport 总墙钟预算耗尽与墙钟守卫线程同族(总时长
                # 超预算), 均为真实抛出路径, 按 wall_clock 落账。
                stage="wall_clock",
            )
        # provider 流里可能混入坏字节/非 UTF-8 切片(分块边界把多字节字符截断),
        # 用 errors="replace" 兜底,不让单行解码异常崩掉整条流式响应。
        line = raw_line.decode("utf-8", "replace").strip()
        if _is_sse_data_line(line):
            on_data_line()
            idle_deadline = _stream_deadline(timeout)
            yield line[5:].strip()


def _is_sse_data_line(line: str) -> bool:
    """Return True for SSE data lines; the caller handles terminal markers."""
    return bool(line and line.startswith("data:"))
