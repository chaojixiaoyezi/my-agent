# LLM: HTTP 唯一传输入口维护请求局部期限、重试、有限正文和中断合同；严格 JSON 不改变默认 SSE，联合 gateway_helpers/strict_request 测试。
# 模块用途: 统一模型 HTTP 请求和错误分类；严格调用按绝对期限收口，普通生成保留原重试和流式空闲语义。
from __future__ import annotations

import errno
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
    ProviderConnectionError,
    ProviderContextWindowError,
    ProviderQuotaExhaustedError,
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from .gateway_request_limits import (
    decode_response_json,
    read_response_body,
    remaining_deadline_seconds,
    stdlib_response_socket,
    validate_request_limits,
)

_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429, 502, 503, 504, 529})
_RETRYABLE_HTTP_DELAYS_SECONDS = (2.0, 5.0, 15.0)
_MAX_RETRY_AFTER_SECONDS = 30.0
_NETWORK_IO_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    OSError,
    http.client.IncompleteRead,
)
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


# LLM: 不可变信封独占本请求期限/重试/大小策略；None 保持原生成合同，显式零重试不可被任何传输层重试放大。
# 类用途: 保存一次 provider HTTP 请求及可选严格限制，OAuth/决策调用可禁止重定向以保护绑定凭据。
@dataclass(frozen=True)
class GatewayRequest:
    """Immutable request envelope for one provider HTTP call."""

    api_base: str
    api_key: str
    path: str
    payload: dict[str, Any]
    headers: dict[str, str]
    timeout: float
    connect_timeout: float = 10.0
    # 首个 SSE data 可以包含排队和长 prompt prefill，预算与后续事件空闲分开。
    # None 表示沿用 timeout；该字段只影响当前请求，不能修改共享 backend。
    first_event_timeout: float | None = None
    allow_redirects: bool = True
    deadline: float | None = None
    max_retries: int | None = None
    max_response_bytes: int | None = None

    # LLM: 新限制在信封创建时校验，不修改共享 backend 或普通请求默认值；校验失败不产生网络副作用。
    # 函数用途: 拒绝非法绝对期限、重试计数或响应上限。
    def __post_init__(self) -> None:
        validate_request_limits(self)

    @property
    def url(self) -> str:
        """Return the final endpoint after joining provider base URL and API path."""
        return self.api_base + self.path


# LLM: 观测开关仅作用当前 provider 线程；退出恢复嵌套作用域，不修改共享模型配置。
# 函数用途: 绑定 HTTP 尝试观察者，可附加无正文的请求前缀摘要。
@contextmanager
def provider_attempt_observer(callback, *, cache_diagnostics: bool = False):
    """Observe physical inference HTTP attempts in the current provider thread."""

    previous = getattr(_PROVIDER_ATTEMPT_OBSERVER, "callback", None)
    previous_diagnostics = getattr(_PROVIDER_ATTEMPT_OBSERVER, "cache_diagnostics", False)
    _PROVIDER_ATTEMPT_OBSERVER.cache_diagnostics = cache_diagnostics
    _PROVIDER_ATTEMPT_OBSERVER.callback = callback
    try:
        yield
    finally:
        _PROVIDER_ATTEMPT_OBSERVER.cache_diagnostics = previous_diagnostics
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


# LLM: Non-streaming POST owns one body read; truncated HTTP bodies become typed transient errors for the existing model retry, never partial JSON success.
# 函数用途: 发送非流式 JSON 请求；停止时关闭响应，断流时交回现有恢复链，不把半截正文当成功。
# LLM: 真机证据(2026-09-11 四路复刻验收): 上游在持续并发下会返回既无 type/code 也无 message 的
# 空洞 400（body 形如 {"object":"error","model":"..."}），同一份 payload 稍后重放即成功，
# 属于供应商侧瞬时拒绝而不是"请求被永久拒绝"。此前该错误直接终结子代理，41 个子代理里 9 个因此死亡、
# 父代理整轮卡住。这里只对这一种客观形状做**有界**重试：最多一次，且必须同时满足
# ①HTTP 400 ②body 里没有 message/type/code 三个定位字段 ③没有任何工具已在本轮执行。
# 不改变其它 400 的"不重试"语义，不重试未知错误，也不掩盖 typed 错误。
_UNLABELED_REJECTION_ATTEMPTS = 2


# 函数用途: 判断一次 400 是否属于"供应商没给任何定位信息的瞬时拒绝"。
def _is_unlabeled_provider_rejection(exc: urllib.error.HTTPError) -> bool:
    if int(getattr(exc, "code", 0) or 0) != 400:
        return False
    try:
        payload = _provider_error_payload(_http_error_detail(exc))
    except Exception:
        return False
    if not isinstance(payload, dict) or not payload:
        return False
    # 供应商两种真实形状都要认：顶层空洞（{"object","model"}）与嵌套 error 对象。
    nested = payload.get("error")
    scopes = [payload, nested] if isinstance(nested, dict) else [payload]
    for scope in scopes:
        if any(str(scope.get(key) or "").strip() for key in ("message", "type", "code")):
            return False
    return True


# 函数用途: 对"无定位信息的 400"做一次有界重试，其它异常原样抛出。
def _retry_unlabeled_rejection(operation):
    last: urllib.error.HTTPError | None = None
    for attempt in range(_UNLABELED_REJECTION_ATTEMPTS):
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            _dump_provider_rejection(exc)
            last = exc
            if not _is_unlabeled_provider_rejection(exc) or attempt + 1 >= _UNLABELED_REJECTION_ATTEMPTS:
                raise _runtime_http_error(exc) from exc
    raise _runtime_http_error(last) from last  # pragma: no cover - 循环内必已 raise


# LLM: 严格请求从发送前到 JSON 解析后共享绝对 deadline；普通请求保留原两层重试，显式重试只在 open 单层计数。
# 函数用途: 发送并完整解析非流式 JSON，超限/到期拒绝半份或迟到结果，关闭响应后沿原错误类型返回。
def post_json(
    request: GatewayRequest,
) -> dict[str, Any]:
    """POST JSON and normalize provider/network failures into typed exceptions."""
    _require_api_key(request.api_key)
    remaining_deadline_seconds(request.deadline)

    # LLM: 读取只使用原响应 guard 和同一绝对期限；取消关闭连接但不赋予传输层任何业务提交权。
    # 函数用途: 完成一次有界 HTTP 正文读取，所有出口释放当前响应。
    def _once() -> bytes:
        with _gateway_response_scope(_open_gateway_request(request)) as (resp, response_guard):
            with _provider_interrupt_callback(response_guard.abort):
                with _request_deadline_scope(request, response_guard):
                    return read_response_body(resp, request)

    try:
        raw = _retry_unlabeled_rejection(_once) if request.max_retries is None else _once()
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        _dump_provider_rejection(exc)
        raise _runtime_http_error(exc) from exc
    except _NETWORK_IO_ERRORS as exc:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口请求已被用户停止") from exc
        raise _runtime_network_error(exc, request) from exc
    # 严格解码沿原 strict_json，解析错误仍归一为 ProviderResponseError，不把坏响应当任务 bug。
    try:
        return decode_response_json(raw, request)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _runtime_decode_error(exc, request) from exc


# LLM: Metadata GET shares the body-read error/interrupt boundary with inference but must not interpret incomplete HTTP bytes as capability evidence.
# 函数用途: 读取模型元数据；区分用户停止与响应断流，读取不完整时不返回错误的能力结论。
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
        _dump_provider_rejection(exc)
        raise _runtime_http_error(exc) from exc
    except _NETWORK_IO_ERRORS as exc:
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


# LLM: SSE iterators share the typed body-read boundary; IncompleteRead must reach the existing model retry, while user cancellation remains a non-retryable interrupt.
# 函数用途: 统一 SSE 异常；把 HTTP 半途断流归到网络恢复，用户停止不进入重连。
def _post_stream_lines(request: GatewayRequest) -> Iterator[str]:
    """Shared streaming implementation used by list and iterator callers."""
    request.payload["stream"] = True
    _require_api_key(request.api_key)
    try:
        yield from _stream_with_watchdog(request)
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        _dump_provider_rejection(exc)
        raise _runtime_http_error(exc) from exc
    except (*_NETWORK_IO_ERRORS, ValueError) as exc:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口流式请求已被用户停止") from exc
        if _is_timeout_exception(exc):
            raise _stream_timeout_error(request, "first_event") from exc
        raise _runtime_network_error(exc, request) from exc


# LLM: Valid SSE data resets rolling idle; teardown IncompleteRead preserves the watchdog's typed phase before outer transport normalization.
# 函数用途: 读取模型流并及时收回超时连接；因超时关闭产生的断流仍记真实超时阶段。
def _stream_with_watchdog(request: GatewayRequest) -> Iterator[str]:
    with _gateway_response_scope(_open_gateway_request(request)) as (resp, response_guard):
        with _provider_interrupt_callback(response_guard.abort):
            if _provider_is_interrupted():
                raise InterruptedError("模型接口流式请求已被用户停止")
            # 会话运行时 对每次 stream.next() 使用 idle timeout：首包可按 prompt
            # 大小给更长预算，首条有效 data 到达后统一回到常规空闲窗口。
            # 持续有效 data 不得再被整次请求的总墙钟误杀；真正停止由 /stop、
            # provider 断流或完整空闲窗口负责。
            initial_deadline = time.monotonic() + _stream_first_event_timeout(request)
            watchdog = _StreamIdleWatchdog(
                response_guard,
                request.timeout,
                idle_deadline=initial_deadline,
            )
            watchdog.start()

            # LLM: Only a valid SSE data record transitions the request from prefill budget to steady-state idle.
            # 函数用途: 首条及后续有效 data 到达时同时刷新 watchdog，并把 socket 切回常规空闲超时。
            def _touch_data_line() -> None:
                watchdog.touch()
                _set_response_read_timeout(resp, request.timeout)

            try:
                yield from _iter_sse_data_lines(
                    resp,
                    timeout=request.timeout,
                    url=request.url,
                    on_data_line=_touch_data_line,
                    initial_deadline=initial_deadline,
                )
                if watchdog.timed_out:
                    raise _stream_timeout_error(request, watchdog.timeout_stage)
            except (*_NETWORK_IO_ERRORS, ValueError) as exc:
                if watchdog.timed_out:
                    raise _stream_timeout_error(request, watchdog.timeout_stage) from exc
                if _is_timeout_exception(exc):
                    raise _stream_timeout_error(request, watchdog.timeout_stage) from exc
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
                    raise _stream_timeout_error(request, watchdog.timeout_stage) from exc
                if _provider_is_interrupted():
                    raise InterruptedError("模型接口流式请求已被用户停止") from exc
                raise
            finally:
                watchdog.cancel()


# LLM: watchdog 只关闭当前 guard；SSE 有效 data 后滚动续期，严格 JSON 不调用 touch，始终使用原绝对期限。
# 类用途: 共用网络等待计时器，分别执行流式空闲和显式非流式总期限合同。
class _StreamIdleWatchdog:
    """Close one blocked response only after the active phase's full idle interval."""

    # LLM: guard 属于当前响应或 open attempt；严格 JSON 不调用 touch，因此沿用同一个绝对期限而不滚动续期。
    # 函数用途: 建立共用传输 watchdog 的初始期限和清理状态，不在构造时启动计时。
    def __init__(
        self,
        response_guard: _GatewayResponseGuard | _GatewayOpenGuard,
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
        self._seen_data = False

    def start(self) -> None:
        with self._lock:
            self._schedule_locked(self._idle_deadline - time.monotonic())

    # LLM: A touch is valid only for parsed SSE data and atomically switches the watchdog into rolling-idle phase.
    # 函数用途: 记录真实模型事件并延长下一事件的空闲截止时间。
    def touch(self) -> None:
        with self._lock:
            if not self._cancelled:
                self._seen_data = True
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

    # LLM: Stage is derived from typed data observation, never from exception text or elapsed-time guesses.
    # 函数用途: 告诉账本超时发生在首事件前，还是首事件后的流空闲阶段。
    @property
    def timeout_stage(self) -> str:
        """Return whether the blocked read died before or after the first data event."""
        with self._lock:
            return "stream_idle" if self._seen_data else "first_event"

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


# LLM: Convert watchdog/parser phase into the closed timeout-stage vocabulary used by the model-call ledger.
# 函数用途: 为首事件和流空闲生成一致的结构化超时异常与用户可读诊断。
def _stream_timeout_error(request: GatewayRequest, stage: str) -> ProviderTimeoutError:
    if stage == "first_event":
        return ProviderTimeoutError(
            "模型接口等待首个流式事件超时: "
            f"first_event_timeout={_stream_first_event_timeout(request):g}s url={request.url}",
            stage="first_event",
        )
    return ProviderTimeoutError(
        f"模型接口流式响应空闲超时: request_timeout={request.timeout}s url={request.url}",
        stage="stream_idle",
    )


# LLM: 这个 guard 独占响应关闭锁，严格 JSON/SSE/用户取消共用，不能因迁出 socket helper 重建第二套关闭状态。
# 类用途: 串行化正常结束和中断关闭，避免标准库响应在并发 close 时产生竞态。
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

    # LLM: 当前响应的关闭只走这个串行 guard；socket 获取与严格读取共用唯一 helper，不关闭其它请求资源。
    # 函数用途: 先 shutdown 唤醒阻塞读取，再幂等关闭响应并忽略清理噪声。
    def abort(self) -> None:
        """Cancel a blocked read and close exactly once without surfacing cleanup noise."""
        with self._lock:
            if self._closed:
                return
            transport = stdlib_response_socket(self.response)
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


# LLM: JSON open/正文共用既有 watchdog，取消只作用精确 guard；不等待清理线程，不宣称可以中止 DNS 或任意解析代码。
# 函数用途: 用不滚动的绝对期限关闭正在阻塞的连接，退出时取消计时并拒绝迟到结果。
@contextmanager
def _request_deadline_scope(request: GatewayRequest, guard: Any):
    if request.deadline is None:
        yield
        return
    remaining_deadline_seconds(request.deadline)
    watchdog = _StreamIdleWatchdog(guard, request.timeout, idle_deadline=request.deadline)
    watchdog.start()
    try:
        yield
    except Exception:
        _check_gateway_deadline(request, guard)
        raise
    else:
        _check_gateway_deadline(request, guard)
    finally:
        watchdog.cancel()


# LLM: 计时器尚未获得调度也必须关闭到期连接；取消优先，由既有 guard 幂等处理与 timer 的关闭竞态。
# 函数用途: 在 HTTP 阶段退出时拒绝迟到结果并释放仍归当前请求所有的连接。
def _check_gateway_deadline(request: GatewayRequest, guard: Any) -> None:
    try:
        remaining_deadline_seconds(request.deadline)
    except (ProviderTimeoutError, InterruptedError):
        guard.abort()
        raise


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


# LLM: 显式 max_retries 是整次 HTTP 调用的唯一重试计数；None 维持普通请求既有策略，期限耗尽前不再发请求。
# 函数用途: 执行有限次 HTTP open，将零重试落实到每一次物理发送之前。
def _open_gateway_request(request: GatewayRequest):
    last_attempt = len(_RETRYABLE_HTTP_DELAYS_SECONDS) if request.max_retries is None else request.max_retries
    for attempt in range(last_attempt + 1):
        remaining_deadline_seconds(request.deadline)
        response = _gateway_request_attempt(request, attempt, last_attempt)
        if response is not None:
            return response
    raise RuntimeError("unreachable gateway retry state")


# LLM: 每次物理 open 都发布观察事实；显式重试合并无定位 400 与网络重试，严格错误正文也不能绕过 deadline/大小上限。
# 函数用途: 发送一次连接及响应头请求，按当前信封限制登记失败或有限退避，不重放半截正文。
def _gateway_request_attempt(request: GatewayRequest, attempt: int, last_attempt: int):
    req = _urllib_request(request)
    remaining_deadline_seconds(request.deadline)
    attempt_id = f"provider-http:{time.time_ns()}:{attempt + 1}"
    base_event = {
        "attempt_id": attempt_id,
        "method": "POST",
        "path": request.path,
    }
    if getattr(_PROVIDER_ATTEMPT_OBSERVER, "cache_diagnostics", False):
        from .cache_diagnostics import request_surface

        base_event["request_surface"] = request_surface(request.payload, request.url)
    _emit_provider_attempt({**base_event, "status": "started"})
    try:
        response = _gateway_urlopen(req, request)
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        retry_scheduled = _request_http_retry(exc, request, attempt, last_attempt)
        retry_wait_seconds = _retry_delay_seconds(exc, attempt) if retry_scheduled else 0.0
        _emit_provider_attempt(
            {
                **base_event,
                "status": "failed",
                "http_status": int(getattr(exc, "code", 0) or 0),
                "error_type": type(exc).__name__,
                "retry_scheduled": retry_scheduled,
                "retry_attempt": attempt + 1 if retry_scheduled else 0,
                "retry_total": last_attempt,
                "retry_wait_seconds": retry_wait_seconds,
            }
        )
        if not retry_scheduled:
            raise
        _request_retry_wait(request, retry_wait_seconds)
        return None
    except _NETWORK_IO_ERRORS as exc:
        retry_scheduled = _should_retry_network_error(exc, attempt, last_attempt)
        retry_wait_seconds = _network_retry_delay_seconds(attempt) if retry_scheduled else 0.0
        _emit_provider_attempt(
            {
                **base_event,
                "status": "failed",
                "error_type": type(exc).__name__,
                "retry_scheduled": retry_scheduled,
                "retry_attempt": attempt + 1 if retry_scheduled else 0,
                "retry_total": last_attempt,
                "retry_wait_seconds": retry_wait_seconds,
            }
        )
        if not retry_scheduled:
            raise
        _request_retry_wait(request, retry_wait_seconds)
        return None
    _emit_provider_attempt(
        {
            **base_event,
            "status": "response_opened",
            "http_status": int(getattr(response, "status", 0) or 0),
        }
    )
    return response


# LLM: 重试退避不得绕过请求绝对期限；仍复用原可中断等待，不新增定时任务或给下一次重置预算。
# 函数用途: 最多等待剩余预算，退避期间到期就结束整次请求。
def _request_retry_wait(request: GatewayRequest, seconds: float) -> None:
    remaining = remaining_deadline_seconds(request.deadline)
    _provider_retry_wait(min(seconds, remaining) if remaining is not None else seconds)
    remaining_deadline_seconds(request.deadline)


# LLM: 零重试错误按状态立即结束，不读取网络诊断正文，关闭噪声不得覆盖原 HTTP 状态；有预算的正文同样受限。
# 函数用途: 清理当前 HTTP 错误响应，并在唯一物理尝试循环决定是否重试。
def _request_http_retry(exc: urllib.error.HTTPError, request: GatewayRequest, attempt: int, last_attempt: int) -> bool:
    if request.max_retries == 0:
        if not isinstance(getattr(exc, _HTTP_ERROR_DETAIL_ATTR, None), str):
            setattr(exc, _HTTP_ERROR_DETAIL_ATTR, "")
        _GatewayResponseGuard(exc).abort()
        return False
    if request.deadline is not None or request.max_response_bytes is not None:
        with _gateway_response_scope(exc) as (response, guard):
            with _provider_interrupt_callback(guard.abort), _request_deadline_scope(request, guard):
                body = read_response_body(response, request)
                setattr(exc, _HTTP_ERROR_DETAIL_ATTR, body.decode("utf-8", "replace"))
    retry = _should_retry_http_error(exc, attempt, last_attempt)
    if request.max_retries is not None and attempt < last_attempt:
        retry = retry or _is_unlabeled_provider_rejection(exc)
    return retry


# LLM: JSON/GET/SSE 共用 open 中断边界；期限复核替代已返回响应时仍须关闭它，HTTPError 未被替代则交原分类；DNS 不能强停。
# 函数用途: 按连接/读取和可选总期限发送请求，释放停止或到期后才返回的当前响应，保留默认生成合同。
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
    read_timeout = _request_initial_read_timeout(request)
    open_guard = _GatewayOpenGuard()
    transport_options = _SplitTimeoutOptions(connect_timeout, read_timeout, open_guard, request.deadline)
    handlers = [
        _provider_proxy_handler(req.full_url),
        _SplitTimeoutHTTPHandler(transport_options),
        _SplitTimeoutHTTPSHandler(transport_options),
    ]
    if not request.allow_redirects:
        from .oauth_transport import NoAuthRedirect

        handlers.append(NoAuthRedirect())
    opener = urllib.request.build_opener(*handlers)
    response = None
    try:
        with _provider_interrupt_callback(open_guard.abort), _request_deadline_scope(request, open_guard):
            if _provider_is_interrupted():
                raise InterruptedError("模型接口请求已被用户停止")
            try:
                response = opener.open(req, timeout=read_timeout)
            except urllib.error.HTTPError as exc:
                response = exc
                raise
            if _provider_is_interrupted():
                open_guard.abort()
                raise InterruptedError("模型接口请求已被用户停止")
            return response
    except Exception as exc:
        if response is not None and response is not exc:
            _GatewayResponseGuard(response).abort()
        if _provider_is_interrupted():
            raise InterruptedError("模型接口请求已被用户停止") from exc
        raise
    finally:
        open_guard.release()


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


# LLM: 严格请求连接窗口不得超过剩余期限；普通请求保留既有最小连接等待以维持默认模型语义。
# 函数用途: 将连接等待限制在本请求允许的读取/总期限之内。
def _bounded_connect_timeout(request: GatewayRequest) -> float:
    read_timeout = _request_initial_read_timeout(request)
    configured = max(0.2, float(request.connect_timeout or 0))
    return min(configured, read_timeout)


# LLM: 严格请求使用原始小数秒及剩余 deadline，不经过 SSE 的一秒下限；None 保持既有首包和滚动 idle 语义。
# 函数用途: 计算当前响应头允许等待的时间，不延长短期限，也不修改共享 backend。
def _request_initial_read_timeout(request: GatewayRequest) -> float:
    remaining = remaining_deadline_seconds(request.deadline)
    if remaining is not None:
        configured = max(float(request.timeout), float(request.first_event_timeout or 0))
        return min(remaining, configured)
    idle_timeout = _stream_deadline_offset(request.timeout)
    if request.first_event_timeout is None:
        return idle_timeout
    return max(idle_timeout, _stream_deadline_offset(request.first_event_timeout))


# LLM: SSE 首个有效 data 后按原 idle 规则更新 socket；读取实际 socket 与严格 JSON 共用唯一 helper，不改变流总时长。
# 函数用途: 首包到达后把 socket 读取超时切回常规流式空闲窗口。
def _set_response_read_timeout(response: Any, timeout: int | float) -> None:
    transport = stdlib_response_socket(response)
    if transport is None:
        return
    try:
        transport.settimeout(_stream_deadline_offset(timeout))
    except (OSError, ValueError):
        return


# LLM: open guard 只保存当前 attempt 的连接引用并串行化 abort/release；它不能跨 attempt 复用或替代 response guard。
# 类用途: 在 HTTPResponse 尚未创建时，也能从中断线程关闭当前连接和 socket。
class _GatewayOpenGuard:
    # LLM: 新 guard 初始未中断且未移交；锁保护连接构造、停止回调和响应移交之间的竞态。
    # 函数用途: 创建一次 provider open 阶段的连接守卫。
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: Any | None = None
        self._aborted = False
        self._released = False

    # LLM: attach 只接受当前 attempt 创建的连接；若停止已先到达，连接必须在锁外立即关闭。
    # 函数用途: 登记 urllib 刚创建、可能正等待连接或响应头的 HTTP 连接。
    def attach(self, connection: Any) -> None:
        abort_now = False
        with self._lock:
            if self._released:
                return
            self._connection = connection
            abort_now = self._aborted
        if abort_now:
            _abort_http_connection(connection)

    # LLM: abort 是幂等中断回调；它只关闭已绑定的本 attempt 连接，且不能持锁调用第三方 close。
    # 函数用途: 关闭尚在 connect/TLS/响应头阶段的 provider 连接以唤醒模型线程。
    def abort(self) -> None:
        connection: Any | None
        with self._lock:
            self._aborted = True
            connection = self._connection
        if connection is not None:
            _abort_http_connection(connection)

    # LLM: release 表示 HTTPResponse 已移交给 response guard 或 open 已失败；后续 callback 不得再碰该连接。
    # 函数用途: 清除 open 阶段连接引用并结束守卫生命周期。
    def release(self) -> None:
        with self._lock:
            self._released = True
            self._connection = None

    # LLM: aborted 是只读 typed flag，连接线程用它拒绝在停止后继续建立或移交传输。
    # 函数用途: 判断当前 provider open attempt 是否已收到中断。
    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted


# LLM: 连接关闭必须先 shutdown socket 再调用 HTTPConnection.close，确保另一个线程阻塞在 getresponse/read 时被唤醒。
# 函数用途: 尽力终止一个 urllib/http.client 连接且吞掉清理噪声。
def _abort_http_connection(connection: Any) -> None:
    transport = getattr(connection, "sock", None)
    if isinstance(transport, socket.socket):
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass
    try:
        connection.close()
    except Exception:
        pass


# LLM: 双超时、可选绝对期限和 attempt-local guard 向 urllib 连接原样传递，连接完成后只使用剩余时间。
# 类用途: 汇总一次 provider open 的连接/读取限制及精确中断句柄。
@dataclass(frozen=True)
class _SplitTimeoutOptions:
    connect_timeout: float
    read_timeout: float
    open_guard: _GatewayOpenGuard | None
    deadline: float | None = None


# LLM: HTTP 连接必须绑定本 attempt guard，显式总期限不得在连接后重新获得完整读取预算。
# 类用途: 为 HTTP provider 分离连接和读取限制，保留当前连接取消与总期限事实。
class _SplitTimeoutHTTPConnection(http.client.HTTPConnection):
    # LLM: guard/deadline 只属于当前 attempt；None 保留普通请求合同，不能跨请求共享取消状态。
    # 函数用途: 初始化带双超时和可选绝对期限的 HTTPConnection。
    def __init__(
        self,
        host: str,
        port: int | None = None,
        timeout: object | None = None,
        source_address: tuple[str, int] | None = None,
        blocksize: int = 8192,
        *,
        transport_options: _SplitTimeoutOptions,
    ):
        del timeout
        self._provider_read_timeout = transport_options.read_timeout
        self._provider_deadline = transport_options.deadline
        super().__init__(
            host,
            port=port,
            timeout=transport_options.connect_timeout,
            source_address=source_address,
            blocksize=blocksize,
        )
        self._provider_open_guard = transport_options.open_guard
        if transport_options.open_guard is not None:
            transport_options.open_guard.attach(self)

    # LLM: connect 前后检查 typed abort；已花在 DNS/connect 的时间不会补回读取窗口，迟到连接不能移交。
    # 函数用途: 建立 HTTP 连接，成功后按实际剩余期限设置读取超时。
    def connect(self) -> None:
        if self._provider_open_guard is not None and self._provider_open_guard.aborted:
            raise InterruptedError("模型接口请求已被用户停止")
        super().connect()
        if self._provider_open_guard is not None and self._provider_open_guard.aborted:
            _abort_http_connection(self)
            raise InterruptedError("模型接口请求已被用户停止")
        if self.sock is not None:
            remaining = remaining_deadline_seconds(self._provider_deadline)
            self.sock.settimeout(min(self._provider_read_timeout, remaining) if remaining is not None else self._provider_read_timeout)


# LLM: HTTPS 与 HTTP 共用 guard/deadline 合同，TLS 完成后读取窗口仍受同一个绝对期限约束。
# 类用途: 为 HTTPS provider 分离连接和读取限制，暴露当前 TLS 连接的中断句柄。
class _SplitTimeoutHTTPSConnection(http.client.HTTPSConnection):
    # LLM: guard/deadline 只绑定本 attempt；TLS context 和代理隧道继续沿标准库入口，不复制第二传输链。
    # 函数用途: 初始化带 TLS、双超时和可选绝对期限的 HTTPSConnection。
    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: object | None = None,
        source_address: tuple[str, int] | None = None,
        context: ssl.SSLContext | None = None,
        blocksize: int = 8192,
        transport_options: _SplitTimeoutOptions,
    ):
        del timeout
        self._provider_read_timeout = transport_options.read_timeout
        self._provider_deadline = transport_options.deadline
        super().__init__(
            host,
            port=port,
            timeout=transport_options.connect_timeout,
            source_address=source_address,
            context=context,
            blocksize=blocksize,
        )
        self._provider_open_guard = transport_options.open_guard
        if transport_options.open_guard is not None:
            transport_options.open_guard.attach(self)

    # LLM: TLS 完成后复核 abort 和剩余 deadline，不能把停止或总期限过后才建成的连接移交。
    # 函数用途: 建立 HTTPS/TLS 连接并按本次剩余期限设置读取超时。
    def connect(self) -> None:
        if self._provider_open_guard is not None and self._provider_open_guard.aborted:
            raise InterruptedError("模型接口请求已被用户停止")
        super().connect()
        if self._provider_open_guard is not None and self._provider_open_guard.aborted:
            _abort_http_connection(self)
            raise InterruptedError("模型接口请求已被用户停止")
        if self.sock is not None:
            remaining = remaining_deadline_seconds(self._provider_deadline)
            self.sock.settimeout(min(self._provider_read_timeout, remaining) if remaining is not None else self._provider_read_timeout)


# LLM: handler 只把双超时和同一 open guard 注入 urllib 创建的 HTTPConnection；不持有第二取消状态。
# 类用途: 为 urllib 普通 HTTP 请求选择可中断的连接实现。
class _SplitTimeoutHTTPHandler(urllib.request.HTTPHandler):
    # LLM: guard 必须来自 `_gateway_urlopen` 当前 attempt，不能使用模块级共享对象。
    # 函数用途: 保存本次 HTTP open 的连接参数。
    def __init__(
        self,
        transport_options: _SplitTimeoutOptions,
    ):
        super().__init__()
        self.transport_options = transport_options

    # LLM: do_open 参数必须携带 open_guard，确保连接构造一发生就可被外层 typed interrupt 访问。
    # 函数用途: 用自定义普通 HTTPConnection 打开请求。
    def http_open(self, req):
        return self.do_open(
            _SplitTimeoutHTTPConnection,
            req,
            transport_options=self.transport_options,
        )


# LLM: HTTPS handler 与 HTTP handler 共用 open guard 协议，并继续传递 urllib 管理的 SSL context。
# 类用途: 为 urllib HTTPS 请求选择可中断的双超时连接实现。
class _SplitTimeoutHTTPSHandler(urllib.request.HTTPSHandler):
    # LLM: guard 生命周期归 `_gateway_urlopen`；handler 只保存引用到一次 opener.open 结束。
    # 函数用途: 保存本次 HTTPS open 的连接参数。
    def __init__(
        self,
        transport_options: _SplitTimeoutOptions,
    ):
        super().__init__()
        self.transport_options = transport_options

    # LLM: do_open 必须把同一 guard 和 SSL context 一起传给连接，不得在 handler 内另建取消 token。
    # 函数用途: 用自定义 HTTPSConnection 打开请求。
    def https_open(self, req):
        return self.do_open(
            _SplitTimeoutHTTPSConnection,
            req,
            context=self._context,
            transport_options=self.transport_options,
        )


# LLM: HTTP retry follows explicit status/quota facts; a missing explanation cannot upgrade 400
# into a transient failure or trigger repeated expensive model submissions.
# 函数用途: 根据 HTTP 状态决定现有有界退避；错误正文缺少说明不代表可以重试。
def _should_retry_http_error(exc: urllib.error.HTTPError, attempt: int, last_attempt: int) -> bool:
    code = int(getattr(exc, "code", 0) or 0)
    if code == 429 and _provider_error_indicates_quota_exhausted(_http_error_detail(exc)):
        return False
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


# LLM: Typed provider facts own recovery; unknown 4xx stays rejected with diagnostic details,
# LLM: 供应商 400 的 body 可能只是空洞对象(真机 2026-09-11: {"object":"error","model":...})，
# 结构化错误里拿不到原因；把完整响应头与 body 落到诊断文件，才能定位真实拒绝理由。
# 只在显式设置 MY_AGENT_PROVIDER_DUMP 时写，且不含密钥；写失败绝不反噬主链路。
# 函数用途: 按需记录一次被拒请求的完整供应商响应，供后续定位。
def _dump_provider_rejection(exc: urllib.error.HTTPError) -> None:
    import os

    target = str(os.environ.get("MY_AGENT_PROVIDER_DUMP") or "").strip()
    if not target:
        return
    try:
        headers = {str(key): str(value) for key, value in (getattr(exc, "headers", None) or {}).items()}
        record = {
            "kind": "provider_rejection",
            "status": int(getattr(exc, "code", 0) or 0),
            "response_headers": headers,
            "response_body": _http_error_detail(exc)[:4000],
        }
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


# never transient merely because the provider omitted its explanation.
# 函数用途: 保留请求拒绝、额度、上下文超限与临时服务失败的区别，供主子代理同样处理。
def _runtime_http_error(exc: urllib.error.HTTPError) -> RuntimeError:
    """Classify provider HTTP errors at the backend boundary."""
    detail = _http_error_detail(exc)
    code = int(getattr(exc, "code", 0) or 0)
    if code in _CONTEXT_WINDOW_HTTP_STATUS_CODES and _provider_error_indicates_context_window(
        detail
    ):
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
    return ProviderRequestRejectedError(
        f"HTTP {exc.code}: {detail}",
        status_code=code,
        details={"status_code": code, "provider_error": _provider_error_payload(detail)},
    )


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


# LLM: 网络错误分类只信异常类型/errno 和受控 marker；ECONNREFUSED 必须保持 provider-transient typed 语义供上层退避和恢复。
# 函数用途: 把底层网络异常归一为超时、瞬时供应故障或需要检查配置的连接错误。
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
            f"模型接口 {host} 临时断开、拒绝连接或连接被重置（{request.url}）。"
            "本次请求可由重试/恢复/接管继续处理；"
            f"底层错误: {reason}"
        )
    return ProviderConnectionError(
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


# LLM: 瞬时判定先读 typed IncompleteRead/DNS/ECONNREFUSED，异常名字或 partial 正文不授予重试权；保持既有预算。
# 函数用途: 将真实 HTTP 响应截断和瞬时连接错误送入现有有界重试，不把配置或普通错误文案当断流。
def _is_transient_network_error(exc: BaseException) -> bool:
    if _is_timeout_exception(exc):
        return False
    if any(isinstance(item, http.client.IncompleteRead) for item in _network_exception_chain(exc)):
        return True
    if _is_dns_resolution_exception(exc):
        return True
    if _is_connection_refused_exception(exc):
        return True
    text = _network_error_text(exc).lower()
    return any(marker in text for marker in _RETRYABLE_NETWORK_ERROR_MARKERS)


# LLM: DNS 解析失败可能来自本机 resolver 短暂不可用；只认 typed gaierror 并复用现有有界退避，不能按错误正文扩大分类。
# 函数用途: 识别 urllib reason 或异常链里的 DNS 解析错误，让一次网络抖动不会直接终止长任务。
def _is_dns_resolution_exception(exc: BaseException) -> bool:
    return any(isinstance(item, socket.gaierror) for item in _network_exception_chain(exc))


# LLM: 连接拒绝必须遍历 urllib reason 与 Python exception chaining，但只接受系统异常类型或 ECONNREFUSED 数值。
# 函数用途: 识别被 urllib 等包装过的“端点主动拒绝连接”，避免依赖中英文错误字符串。
def _is_connection_refused_exception(exc: BaseException) -> bool:
    return any(
        isinstance(item, ConnectionRefusedError)
        or getattr(item, "errno", None) == errno.ECONNREFUSED
        for item in _network_exception_chain(exc)
    )


# LLM: 异常链遍历必须有 identity 去重和固定字段集合，不能递归读取任意用户对象属性。
# 函数用途: 展开网络异常本身、reason、cause 和 context，供结构化 errno 分类复用。
def _network_exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    pending = [exc]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        for name in ("reason", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return tuple(result)


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


def _stream_deadline_offset(timeout: int | float) -> float:
    """预算秒数(浮点, 不截断): 最小 1.0s 防 0 预算死循环, 与门槛1/2 账本
    effective 值一致——dynamic 预算可带小数(如 303.1), int() 截断会放大
    0.05/0.5s 级预算(steward seq1533-2)。"""
    return max(1.0, float(timeout or 0))


def _stream_deadline(timeout: int | float) -> float:
    """Convert request timeout seconds into the next monotonic idle deadline."""
    return time.monotonic() + _stream_deadline_offset(timeout)


# LLM: Keep first-event timing request-local so concurrent main/subagent calls cannot overwrite a shared backend timeout.
# 函数用途: 返回当前请求等待首个 SSE data 的预算；未单独配置时沿用常规空闲窗口。
def _stream_first_event_timeout(request: GatewayRequest) -> float:
    """Return the request-local first SSE event budget."""
    return _request_initial_read_timeout(request)


# LLM: Check the current execution's interrupt flag at every SSE boundary before exposing provider data to higher layers.
# 函数用途: 逐行读取 SSE 数据，并在每个安全点检查超时与用户停止。
def _iter_sse_data_lines(
    response,
    *,
    timeout: int | float,
    url: str,
    on_data_line,
    initial_deadline: float | None = None,
) -> Iterator[str]:
    if initial_deadline is None:
        initial_deadline = time.monotonic() + _stream_deadline_offset(timeout)
    idle_deadline = initial_deadline
    seen_data = False
    for raw_line in response:
        if _provider_is_interrupted():
            raise InterruptedError("模型接口流式请求已被用户停止")
        if time.monotonic() > idle_deadline:
            stage = "stream_idle" if seen_data else "first_event"
            label = "流式响应空闲" if seen_data else "等待首个流式事件"
            raise ProviderTimeoutError(
                f"模型接口{label}超时: timeout={timeout}s url={url}",
                stage=stage,
            )
        # provider 流里可能混入坏字节/非 UTF-8 切片(分块边界把多字节字符截断),
        # 用 errors="replace" 兜底,不让单行解码异常崩掉整条流式响应。
        line = raw_line.decode("utf-8", "replace").strip()
        if _is_sse_data_line(line):
            seen_data = True
            on_data_line()
            idle_deadline = _stream_deadline(timeout)
            yield line[5:].strip()


def _is_sse_data_line(line: str) -> bool:
    """Return True for SSE data lines; the caller handles terminal markers."""
    return bool(line and line.startswith("data:"))
