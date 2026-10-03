# LLM: ChatGPT 订阅 Responses 的 WebSocket 传输（与官方命令行 Codex 的默认传输相同，服务商模型目录对这些模型声明
#   prefer_websockets）。同一个 GatewayRequest（地址、认证头、请求体）改用 wss 发送 {"type": "response.create", ...}，
#   逐条产出与 SSE data 相同的 JSON 事件文本，交给 responses_wire.collect_response，不另建解析或状态。
#   09-30 实测：普通 SSE 长输出在服务端中途卡住、约 60 秒后被关（gpt-6-luna 4/4），同一请求走 WebSocket 2/2 完整。
#   超时语义与 SSE 相同：首个事件单独预算，之后按 request.timeout 滚动空闲；/stop 立即关连接并抛 InterruptedError；
#   握手失败沿 HTTP 同一分类（_runtime_http_error / _runtime_network_error）并按同一有限次数重试；握手阶段的超时
#   （TLS/升级握手，response.create 还没发出）照 SSE 归 first_event，交给回合层退避重试，不落 provider_declared；
#   回复完成前断开抛可恢复的 ProviderTransientError（文本带阶段、连接后秒数和关闭码，供排查），交给上层模型重试。
#   实验发送许可不支持（拒绝发送）。
#   与官方 Codex 一致不主动发心跳 ping，只自动回应服务端 ping（09-30 审计：49 次里 2 次在 23–24 秒中途断开，紧跟客户端
#   第 20 秒的 ping）。改动须同步 test_responses_websocket。
# 模块用途: 让 ChatGPT 订阅模型的长回复（含 Compact 摘要）不再中途断线。
from __future__ import annotations

import email.message
import io
import json
import time
import urllib.error
from collections.abc import Iterator

from .errors import ProviderTransientError
from .gateway_helpers import (
    _RETRYABLE_HTTP_DELAYS_SECONDS,
    _RETRYABLE_HTTP_STATUS_CODES,
    GatewayRequest,
    _dump_provider_rejection,
    _emit_provider_attempt,
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
)
from .gateway_request_limits import remaining_deadline_seconds

# Responses WebSocket 协议版本头；与官方命令行使用的取值一致，服务端据此启用 response.create 消息。
BETA_HEADER_VALUE = "responses_websockets=2026-02-06"
_DROPPED_HEADERS = {"content-type", "accept", "content-length", "user-agent", "openai-beta"}
# 与 HTTP 传输的默认 UA 保持一致（gateway_helpers._urllib_request），不冒充其他客户端。
_DEFAULT_USER_AGENT = "my-agent/1.0 (anthropic-compatible client)"
# 终态事件会带回整份请求配置（含 instructions、tools），单条消息可能很大；只防失控，不截断正常回复。
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# 等待消息时每隔这么久醒来一次检查用户停止；连接关闭回调才是主停止路径，这里只是兜底检查点。
_STOP_CHECK_SECONDS = 1.0
_STOPPED_MESSAGE = "模型接口流式请求已被用户停止"
# 这些事件之后服务端不会再发本次回复的内容；产出后即结束，不再等下一条（错误事件由 collect_response 报错）。
_TERMINAL_EVENT_TYPES = frozenset({"response.completed", "response.incomplete", "response.failed", "error"})


# LLM: 只做协议名替换（https→wss、http→ws），路径和查询保持原样；不按域名做任何分支。
# 函数用途: 把 Responses 的 HTTP 地址换成对应的 WebSocket 地址。
def websocket_url(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


# LLM: 生成器：打开连接、发 response.create、逐条产出事件文本；调用方 close() 或异常都会关连接。
#   请求体沿用 payload（含 stream=true），只加 type 字段；发送许可不支持时拒绝，不发任何字节。
# 函数用途: 通过 WebSocket 发一次 Responses 请求，逐条返回服务端事件（JSON 文本）。
def iter_responses_websocket(request: GatewayRequest) -> Iterator[str]:
    if request.send_permit is not None:
        raise ValueError("WebSocket 传输不支持实验发送许可，未发送请求。")
    remaining_deadline_seconds(request.deadline)
    connection = _open_connection(request)
    try:
        with _provider_interrupt_callback(connection.close):
            connection.send(json.dumps({"type": "response.create", **request.payload}, ensure_ascii=False))
            yield from _events(connection, request)
    finally:
        connection.close()


# LLM: 握手失败按 HTTP 同一套可重试状态和网络错误判断；每次尝试都发布 started / failed / response_opened 观察事件，
#   与 HTTP 传输的账本口径一致（method=WEBSOCKET）。用户停止优先，不进入重试。
# 函数用途: 建立 WebSocket 连接，失败时在有限次数内重试，返回已打开的连接。
def _open_connection(request: GatewayRequest):
    last_attempt = len(_RETRYABLE_HTTP_DELAYS_SECONDS) if request.max_retries is None else request.max_retries
    for attempt in range(last_attempt + 1):
        event = {"attempt_id": f"provider-ws:{time.time_ns()}:{attempt + 1}", "method": "WEBSOCKET", "path": request.path}
        _emit_provider_attempt({**event, "status": "started"})
        try:
            connection = _connect(request)
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


# LLM: 首条事件用首包预算，之后每条有效消息把期限滚动到 request.timeout；每秒醒一次检查停止。
#   回复完成前连接断开：用户停止→InterruptedError；否则可恢复的 ProviderTransientError（不当成完整回复），
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
        text = message if isinstance(message, str) else bytes(message).decode("utf-8", "replace")
        yield text
        if _event_type(text) in _TERMINAL_EVENT_TYPES:
            return
        deadline, stage = time.monotonic() + max(1.0, float(request.timeout or 0)), "stream_idle"


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
