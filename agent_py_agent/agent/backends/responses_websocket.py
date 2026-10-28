# LLM: ChatGPT 订阅 Responses 的 WebSocket 传输（与官方命令行 Codex 的默认传输相同，服务商模型目录对这些模型声明
#   prefer_websockets）。同一个 GatewayRequest（地址、认证头、请求体）改用 wss 发送 {"type": "response.create", ...}，
#   逐条产出与 SSE data 相同的 JSON 事件文本，交给 responses_wire.collect_response，不另建解析或状态。
#   09-30 实测：普通 SSE 长输出在服务端中途卡住、约 60 秒后被关（gpt-6-luna 4/4），同一请求走 WebSocket 2/2 完整。
#   超时语义与 SSE 相同：发送/首个事件各有首包预算，之后按 request.timeout 滚动空闲（信封带绝对期限时与
#   request.deadline 取更早者，与 HTTP 路径的硬期限同口径）；/stop 直接关 socket，
#   不等对端关闭握手，再抛 InterruptedError；
#   握手失败沿 HTTP 同一分类（_runtime_http_error / _runtime_network_error）并按同一有限次数重试；握手阶段的超时
#   （TLS/升级握手，response.create 还没发出）照 SSE 归 first_event，交给回合层退避重试，不落 provider_declared；
#   回复完成前断开抛可恢复的 ProviderTransientError（文本带阶段、连接后秒数和关闭码，供排查），交给上层模型重试。
#   实验发送许可不支持（拒绝发送）。
#   与官方 Codex 一致不主动发心跳 ping，只自动回应服务端 ping（09-30 审计：49 次里 2 次在 23–24 秒中途断开，紧跟客户端
#   第 20 秒的 ping）。改动须同步 test_responses_websocket。
#   recv的2秒超时只作兜底，取消仍由当前物理连接close_socket即时唤醒；干净终态先归还请求租约再回会话池。
# 模块用途: 复用隔离的Responses会话连接；空闲不占并发名额，失败丢弃旧链且只在无输出时有界回退。
from __future__ import annotations

import email.message
import io
import json
import time
import urllib.error
from collections.abc import Iterator
from dataclasses import replace
from threading import Event, Timer

from .errors import ProviderResponseError, ProviderTransientError
from .gateway_helpers import (
    _PROVIDER_ATTEMPT_OBSERVER,
    _RETRYABLE_HTTP_DELAYS_SECONDS,
    _RETRYABLE_HTTP_STATUS_CODES,
    GatewayRequest,
    _dump_provider_rejection,
    _emit_provider_attempt,
    _emit_provider_queue,
    _is_timeout_exception,
    _network_retry_delay_seconds,
    _provider_interrupt_callback,
    _provider_is_interrupted,
    _request_retry_wait,
    _runtime_http_error,
    _runtime_network_error,
    _should_retry_network_error,
    _stream_first_event_timeout,
    _stream_timeout_error,
    gateway_request_body,
)
from .gateway_request_limits import remaining_deadline_seconds
from .request_scope import acquire_provider_request, release_provider_response
from .responses_ws_pool import (
    ConnectionEntry,
    ResponsesSessionPool,
    SessionState,
    cache_usage,
    incremental_payload,
    remember_completed,
    session_pool_key,
)

# Responses WebSocket 协议版本头；与官方命令行使用的取值一致，服务端据此启用 response.create 消息。
BETA_HEADER_VALUE = "responses_websockets=2026-02-06"
_DROPPED_HEADERS = {"content-type", "accept", "content-length", "user-agent", "openai-beta"}
# 与 HTTP 传输的默认 UA 保持一致（gateway_helpers._urllib_request），不冒充其他客户端。
_DEFAULT_USER_AGENT = "my-agent/1.0 (anthropic-compatible client)"
# 终态事件会带回整份请求配置（含 instructions、tools），单条消息可能很大；只防失控，不截断正常回复。
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# 等待消息时每隔这么久醒来一次检查用户停止；连接关闭回调才是主停止路径，这里只是兜底检查点。
_STOP_CHECK_SECONDS = 2.0
_STOPPED_MESSAGE = "模型接口流式请求已被用户停止"
# 这些事件之后服务端不会再发本次回复的内容；产出后即结束，不再等下一条（错误事件由 collect_response 报错）。
_TERMINAL_EVENT_TYPES = frozenset({"response.completed", "response.incomplete", "response.failed", "error"})
_POOL = ResponsesSessionPool()


# LLM: Internal control signal consumed only before any output was delivered; provider error text is never used for retry decisions.
# 类用途: 标记结构化previous_response_id失效，供一次新连接全量恢复。
class _PreviousResponseRejected(Exception):
    pass


# LLM: 只做协议名替换（https→wss、http→ws），路径和查询保持原样；不按域名做任何分支。
# 函数用途: 把 Responses 的 HTTP 地址换成对应的 WebSocket 地址。
def websocket_url(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


# LLM: The real collector stops at terminal yield: release/clean-pool return must PRECEDE it. Borrowed sockets are request-exclusive.
#   Recovery is bounded to once, before output, only for structured previous-ID rejection/connection-close; max_retries=0 remains strict.
# 函数用途: 按宿主会话复用连接和增量链，逐请求借还租约，脏连接即时丢弃。
def iter_responses_websocket(request: GatewayRequest, *, provider_id: str = "", idle_ttl_seconds: float = 300,
                             max_idle: int = 32) -> Iterator[str]:
    if request.send_permit is not None:
        raise ValueError("WebSocket 传输不支持实验发送许可，未发送请求。")
    if "previous_response_id" in request.payload:
        raise ValueError("previous_response_id 只能由本次会话连接的完整历史锚点生成。")
    remaining_deadline_seconds(request.deadline)
    key = session_pool_key(request, provider_id)
    state = _POOL.state(key, idle_ttl_seconds, max_idle)
    for attempt in range(2):
        entry, connection, clean, retry, visible, terminal, done = None, None, False, False, False, None, []
        reused, terminal_text = False, ""
        try:
            entry = _POOL.take(key, state) if state is not None and attempt == 0 else None
            if entry is not None and not _socket_clean(entry.connection):
                entry.discard()
                entry = None
            reused = entry is not None
            payload = incremental_payload(entry, request.payload) if entry is not None else request.payload
            sending = _sticky_request(replace(request, payload=payload), state)
            if entry is None:
                connection = _open_connection(sending)
                entry = ConnectionEntry(connection, state or SessionState(0))
                _POOL.capture(key, state, getattr(getattr(connection, "response", None), "headers", None))
            else:
                connection = entry.connection
                connection._provider_request_lease = acquire_provider_request(sending, _emit_provider_queue)
                _reused_attempt(sending)
            with _provider_interrupt_callback(connection.close_socket):
                _send_create_message(connection, sending)
                for text in _events(connection, sending):
                    event = _protocol_event(text)
                    kind = event.get("type")
                    if reused and not visible and _previous_rejected(event) and attempt == 0 and request.max_retries != 0:
                        raise _PreviousResponseRejected()
                    if kind in _TERMINAL_EVENT_TYPES:
                        terminal = event
                        terminal_text = text
                        break
                    if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                        done.append(event["item"])
                    visible = visible or kind not in {"response.created", "response.in_progress", "codex.rate_limits"}
                    yield text
            response = (terminal or {}).get("response", {})
            if not isinstance(response, dict):
                raise ProviderResponseError("Responses 流终态格式无效。")
            if terminal is not None and "response" in terminal and terminal.get("type") in {"response.completed", "response.incomplete"}:
                response["usage"] = cache_usage(response.get("usage"), reused=reused, incremental="previous_response_id" in payload)
                terminal_text = json.dumps(terminal, ensure_ascii=False)
            _raise_if_stopped()
            completed = (terminal or {}).get("type") == "response.completed" and response.get("status") == "completed"
            if completed and state is not None and isinstance(response.get("id"), str) and response["id"] and _socket_clean(connection):
                remember_completed(entry, request.payload, response, done)
                clean = True
        except BaseException as exc:
            clean = False
            retry = reused and not visible and attempt == 0 and request.max_retries != 0 and _recoverable_reuse_error(exc)
            if not retry or _provider_is_interrupted():
                raise
        finally:
            if connection is not None:
                release_provider_response(connection)
                connection._provider_request_lease = None
            if entry is not None:
                if clean and not _provider_is_interrupted():
                    _POOL.put(key, entry, idle_ttl_seconds, max_idle)
                elif state is None:
                    _close_admitted_connection(connection, (terminal or {}).get("type") == "response.completed")
                else:
                    entry.discard()
        if retry:
            continue
        if terminal is not None:
            yield terminal_text
        return


# LLM: A nonblocking read must find NO application frame; any unexpected data, close, or unsupported socket is conservatively dirty.
# 函数用途: 验证终态后及借出前没有未读帧，防止下一请求读到旧数据。
def _socket_clean(connection) -> bool:
    try:
        connection.recv(timeout=0)
    except TimeoutError:
        return True
    except Exception:
        return False
    return False


# LLM: Malformed JSON/object/type is a protocol violation, never a reusable stream or an implicit successful EOF.
# 函数用途: 只接收合法的结构化Responses事件。
def _protocol_event(text: str) -> dict:
    try:
        event = json.loads(text)
    except ValueError as exc:
        raise ProviderResponseError("Responses 流包含无效 JSON。") from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ProviderResponseError("Responses 流事件格式无效。")
    return event


# LLM: Match exact provider codes only; arbitrary error messages never authorize replay of a potentially billed request.
# 函数用途: 判断服务端是否明确拒绝旧response锚点。
def _previous_rejected(event: dict) -> bool:
    error = event.get("error") or (event.get("response") or {}).get("error") or event
    return isinstance(error, dict) and error.get("code") in {"previous_response_not_found", "invalid_previous_response_id"}


# LLM: Timeouts, cancellation and general midstream errors are NOT replayable; only a closed reused connection or explicit bad ID is.
# 函数用途: 区分可安全全量恢复的复用失效与必须向上层报告的失败。
def _recoverable_reuse_error(exc: BaseException) -> bool:
    from websockets.exceptions import ConnectionClosed

    return isinstance(exc, (_PreviousResponseRejected, ConnectionClosed)) or (
        isinstance(exc, ProviderTransientError) and isinstance(exc.__cause__, ConnectionClosed))


# LLM: Never accept an arbitrary sticky input header; only server-origin state for this exact identity may be echoed.
# 函数用途: 给本次握手附加可信路由状态，不修改原请求头。
def _sticky_request(request: GatewayRequest, state: SessionState | None) -> GatewayRequest:
    headers = {name: value for name, value in request.headers.items() if name.lower() != "x-codex-turn-state"}
    if state is not None and state.turn_state_header:
        headers["x-codex-turn-state"] = state.turn_state_header
    return replace(request, headers=headers)


# LLM: Reused physical sends must be visible in the same attempt ledger, with full request-local diagnostic input and no socket headers.
# 函数用途: 为复用连接上的真实发送补齐请求尝试观察事件。
def _reused_attempt(request: GatewayRequest) -> None:
    event = _attempt_event(request, 1)
    _emit_provider_attempt({**event, "status": "started"})
    _emit_provider_attempt({**event, "status": "response_opened", "http_status": 101})


# LLM: Follow the HTTP observer's explicit diagnostics switch; surface contains irreversible summaries, never input text/token identity.
# 函数用途: 生成WebSocket请求尝试的既有账本事件及前缀诊断。
def _attempt_event(request: GatewayRequest, attempt: int) -> dict:
    event = {"attempt_id": f"provider-ws:{time.time_ns()}:{attempt}", "method": "WEBSOCKET", "path": request.path}
    if getattr(_PROVIDER_ATTEMPT_OBSERVER, "cache_diagnostics", False):
        from .cache_diagnostics import request_surface

        event["request_surface"] = request_surface(request.payload, request.url)
    return event


# LLM: Host calls this on explicit same-thread reset; /new gets a different host session key and cannot read old state.
# 函数用途: 清理当前宿主会话的空闲连接、sticky头和增量链。
def reset_responses_session() -> None:
    from .provider_headers import current_provider_session

    session = current_provider_session()
    if session:
        _POOL.close(session)


# LLM: Bind only Responses requests after actual credential refresh, so response callbacks cannot cross a changed account or owner.
# 函数用途: 为HTTP Responses信封附加当前会话路由头及服务端握手头收集器。
def prepare_http_responses_request(request: GatewayRequest, *, provider_id: str = "", idle_ttl_seconds: float = 300,
                                   max_idle: int = 32) -> GatewayRequest:
    key = session_pool_key(request, provider_id)
    state = _POOL.state(key, idle_ttl_seconds, max_idle)

    # LLM: Capture one exact state object; reset/eviction prevents this late response from restoring retired headers.
    # 函数用途: 只回收本请求所属会话的路由状态。
    def capture(headers) -> None:
        _POOL.capture(key, state, headers)

    return replace(_sticky_request(request, state), response_headers_observer=capture if state is not None else None)


# LLM: Legacy explicit-close helper releases the request lease before closing; pooled transport uses its own pre-terminal return path.
# 函数用途: 按完成事实关闭 WebSocket，并及时归还供应商名额而不依赖 GC。
def _close_admitted_connection(connection, completed: bool) -> None:
    release_provider_response(connection)
    connection._provider_request_lease = None
    try:
        if completed:
            connection.close()
        else:
            connection.close_socket()
    finally:
        release_provider_response(connection)


# LLM: 握手失败按 HTTP 同一套可重试状态和网络错误判断；每次尝试都发布 started / failed / response_opened 观察事件，
#   与 HTTP 传输的账本口径一致（method=WEBSOCKET）。用户停止优先，不进入重试。
# 函数用途: 为首次物理请求取得名额并握手；成功名额只活到本次响应终态而非连接寿命。
def _open_connection(request: GatewayRequest):
    last_attempt = len(_RETRYABLE_HTTP_DELAYS_SECONDS) if request.max_retries is None else request.max_retries
    for attempt in range(last_attempt + 1):
        lease = acquire_provider_request(request, _emit_provider_queue)
        event = _attempt_event(request, attempt + 1)
        _emit_provider_attempt({**event, "status": "started"})
        try:
            connection = _connect_admitted(request, lease)
        except Exception as exc:  # noqa: BLE001 握手/网络异常统一交给下面的分类，不吞掉
            retry = _handshake_retry(exc, request, attempt, last_attempt)
            _emit_provider_attempt({**event, "status": "failed", "error_type": type(exc).__name__,
                                    "http_status": _status_code(exc), "retry_scheduled": retry})
            if not retry:
                raise _handshake_error(exc, request) from exc
            _request_retry_wait(request, _network_retry_delay_seconds(attempt))
            continue
        _emit_provider_attempt({**event, "status": "response_opened", "http_status": 101})
        return connection
    raise RuntimeError("unreachable websocket retry state")


# LLM: 每次握手在started前取得本请求名额；失败释放再退避，成功由响应终态归还，空闲连接不保留lease。
# 函数用途: 用已准入名额保护一次 WebSocket 连接，排队不计入握手/首包等待。
def _connect_admitted(request: GatewayRequest, lease):
    try:
        connection = _connect(request)
        if lease is not None:
            connection._provider_request_lease = lease
        return connection
    except BaseException:
        if lease is not None:
            lease.release()
        raise


# LLM: 代理沿环境变量（与 urllib 相同的 HTTPS_PROXY 等），不绕开本机代理；连接超时用 connect_timeout；
#   不开客户端 ping（ping_interval=None，与官方 Codex 相同），服务端 ping 仍由库自动回 pong；死连接靠 _events 的
#   首包/空闲超时收口。认证头来自同一个 GatewayRequest，本函数不读凭据。
# 函数用途: 按请求信封打开一条 WebSocket 连接。
def _connect(request: GatewayRequest):
    from websockets.sync.client import connect

    headers = {name: value for name, value in (request.headers or {}).items() if name.lower() not in _DROPPED_HEADERS}
    headers["OpenAI-Beta"] = BETA_HEADER_VALUE
    user_agent = next((value for name, value in (request.headers or {}).items() if name.lower() == "user-agent"),
                      _DEFAULT_USER_AGENT)
    return connect(websocket_url(request.url), additional_headers=headers, user_agent_header=user_agent,
                   open_timeout=max(1.0, float(request.connect_timeout or 10.0)), max_size=_MAX_MESSAGE_BYTES,
                   ping_interval=None, ping_timeout=None, close_timeout=5)


# LLM: `websockets.sync.Connection.send` 会对普通发送执行 socket.sendall，close_timeout 只约束关闭握手；
#   计时器到期先关当前 socket 唤醒发送线程，超时仍归原 first_event 合同，不新增重试分类。
# 函数用途: 为 response.create 的网络写入复用当前请求首包预算，避免连接建立后卡在发送阶段。
def _send_create_message(connection, request: GatewayRequest) -> None:
    # LLM: WebSocket 首包不经 HTTP（直接 socket.send），编码仍复用唯一编码器 gateway_request_body：
    #   历史结构化对象的键序同样受磁盘重排影响，与 HTTP 路径共用「同一语义 → 同一字节」口径。
    #   编码器用 json.dumps 默认转义（含中文时输出 \uXXXX，JSON 解析语义等价）；旧实现
    #   ensure_ascii=False 只在切换瞬间产生一次性字节变化，不改变服务端可见内容或 token。
    #   改首包格式须同步 test_responses_websocket.py 的首包用例。
    message = gateway_request_body({"type": "response.create", **request.payload}).decode("utf-8")
    timed_out = Event()
    timer = Timer(_stream_first_event_timeout(request), _expire_websocket_send, args=(connection, timed_out))
    timer.daemon = True
    timer.start()
    try:
        connection.send(message)
    except Exception as exc:
        if timed_out.is_set():
            raise _stream_timeout_error(request, "first_event") from exc
        raise
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise _stream_timeout_error(request, "first_event")


# LLM: 只触碰当前发送连接；置位后用库提供的 close_socket 直接 shutdown/close，避免拿协议锁等待对端回应。
# 函数用途: 首包发送预算耗尽时关掉当前 WebSocket，唤醒阻塞的 sendall。
def _expire_websocket_send(connection, timed_out: Event) -> None:
    timed_out.set()
    try:
        connection.close_socket()
    except Exception:
        return


# LLM: 首条事件用首包预算，之后每条有效消息把期限滚动到 request.timeout——信封带绝对期限
#   （request.deadline，cabfix 起辅助调用会给）时与它取更早者，与 HTTP 路径 _StreamIdleWatchdog 的
#   hard_deadline 同口径：持续有效事件也不能越过绝对期限；None 时行为与旧版完全一致。
#   每秒醒一次检查停止；回复完成前连接断开：用户停止→InterruptedError；否则可恢复的 ProviderTransientError（不当成完整回复），
#   文本附当前阶段（first_event/stream_idle）、连接后秒数和关闭帧信息，只作诊断，不参与任何判定。
#   终态事件（completed/incomplete/failed/error）产出后即结束，只读结构化 type 字段判断。
# 函数用途: 逐条读取服务端事件，处理空闲超时、用户停止和中途断开。
def _events(connection, request: GatewayRequest) -> Iterator[str]:
    from websockets.exceptions import ConnectionClosed

    started = time.monotonic()
    deadline, stage = started + _stream_first_event_timeout(request), "first_event"
    while True:
        try:
            message = connection.recv(timeout=max(0.01, min(_STOP_CHECK_SECONDS, deadline - time.monotonic())))
        except TimeoutError:
            _raise_if_stopped()
            if time.monotonic() >= deadline:
                raise _stream_timeout_error(request, stage) from None
            continue
        except ConnectionClosed as exc:
            _raise_if_stopped(exc)
            raise ProviderTransientError(
                f"网络请求失败: 模型接口 WebSocket 在回复完成前断开（{request.url}；阶段 {stage}，连接后 "
                f"{time.monotonic() - started:.1f} 秒，{_close_detail(exc)}）。本次请求可由重试/恢复/接管继续处理；"
                f"底层错误: {type(exc).__name__}") from exc
        _raise_if_stopped()
        # LLM: 期限检查不能只放在 recv 超时分支：服务端持续滴事件时 recv 每次都成功，跳过检查会让
        #   绝对期限永远不生效（wsdeadline 用例实测）。这里对每条已收消息也检查一次当前阶段截止。
        if time.monotonic() >= deadline:
            raise _stream_timeout_error(request, stage) from None
        text = message if isinstance(message, str) else bytes(message).decode("utf-8", "replace")
        yield text
        if _event_type(text) in _TERMINAL_EVENT_TYPES:
            return
        idle_deadline = time.monotonic() + max(1.0, float(request.timeout or 0))
        deadline = idle_deadline if request.deadline is None else min(idle_deadline, request.deadline)
        stage = "stream_idle"


# LLM: 只取 websockets 关闭异常上的结构化关闭帧（先看服务端 rcvd，再看本端 sent）；原因文本截到 120 字。
#   没有关闭帧（例如 TCP 直接断开）写“无关闭帧”。只拼诊断文字，不参与重试或状态判定。
# 函数用途: 把连接关闭信息整理成一句话，方便排查是谁、以什么码关的连接。
def _close_detail(exc: BaseException) -> str:
    for side, frame in (("服务端", getattr(exc, "rcvd", None)), ("本端", getattr(exc, "sent", None))):
        if frame is not None:
            reason = str(getattr(frame, "reason", "") or "")[:120]
            return f"{side}关闭码 {int(getattr(frame, 'code', 0) or 0)}" + (f"，原因 {reason}" if reason else "")
    return "无关闭帧"


# LLM: 只读事件的 type 字段；坏 JSON 返回空串（照常交给 collect_response，由它报无效 JSON）。
# 函数用途: 取一条服务端事件的类型。
def _event_type(text: str) -> str:
    try:
        event = json.loads(text)
    except ValueError:
        return ""
    return str(event.get("type") or "") if isinstance(event, dict) else ""


# LLM: 用户停止是唯一不重试的中断；原异常作为 cause 保留。
# 函数用途: 已收到停止信号时抛出统一的停止异常。
def _raise_if_stopped(cause: BaseException | None = None) -> None:
    if _provider_is_interrupted():
        raise InterruptedError(_STOPPED_MESSAGE) from cause


# LLM: 状态码握手失败按 HTTP 同一可重试集合；其余走网络错误的瞬时判断。用户停止不重试。
# 函数用途: 判断一次握手失败是否要重试。
def _handshake_retry(exc: BaseException, request: GatewayRequest, attempt: int, last_attempt: int) -> bool:
    if _provider_is_interrupted() or attempt >= last_attempt or request.max_retries == 0:
        return False
    status = _status_code(exc)
    if status:
        return status in _RETRYABLE_HTTP_STATUS_CODES or status >= 500
    return _should_retry_network_error(exc, attempt, last_attempt)


# LLM: 带状态码的握手失败转成 urllib HTTPError 交给 _runtime_http_error，额度/上下文/拒绝/临时故障与 HTTP 传输同一分类；
#   无状态码的失败里，超时（TLS 握手 / 升级握手，response.create 还没发出）照 SSE 归 first_event，交给回合层退避重试，
#   避免 provider_declared 被生成层立刻重试一次、再超时整轮失败；其余交给 _runtime_network_error。用户停止优先。
#   这里显式标注 wait_phase="handshake"：stage 仍按修法 A 保持 first_event（回归规则不变），
#   只让账本事后能把「握手没连上」和「连上了但首个事件超时」分开统计。
# 函数用途: 把握手失败换成产品统一的服务商错误。
def _handshake_error(exc: BaseException, request: GatewayRequest) -> BaseException:
    if _provider_is_interrupted():
        return InterruptedError(_STOPPED_MESSAGE)
    response = getattr(exc, "response", None)
    if not _status_code(exc) or response is None:
        if _is_timeout_exception(exc):
            return _stream_timeout_error(request, "first_event", wait_phase="handshake")
        return _runtime_network_error(exc, request)
    headers = email.message.Message()
    for name, value in response.headers.raw_items():
        headers[name] = value
    error = urllib.error.HTTPError(request.url, int(response.status_code), str(response.reason_phrase or ""), headers,
                                   io.BytesIO(bytes(response.body or b"")))
    _dump_provider_rejection(error)
    return _runtime_http_error(error)


# LLM: 只认 websockets InvalidStatus 附带的响应状态码；没有就返回 0（网络类失败）。
# 函数用途: 取握手失败的 HTTP 状态码。
def _status_code(exc: BaseException) -> int:
    response = getattr(exc, "response", None)
    return int(getattr(response, "status_code", 0) or 0)
