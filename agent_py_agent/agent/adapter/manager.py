

from __future__ import annotations

"""LLM: ChannelManager persists authenticated adapter ingress before any Gateway or provider IO.

模块用途: 注册外部通道，并把入站消息交给可恢复的单线程投递状态机处理。
"""

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..delivery import ChannelAdapterRegistry, DeliveryContext, DeliveryService, ReplyEnvelope
from .base import BaseChannelAdapter
from .delivery import (
    GatewayControlReceiptResult,
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    GatewayReplyQuarantineError,
    GatewayReplyReceiptCallbacks,
    PendingGatewayReply,
)
from .ingress import (
    GatewayAdapterDeliveryWorker,
    GatewayIngressAdvance,
    GatewayIngressRecord,
    GatewayIngressStore,
    build_gateway_ingress_record,
)
from .protocol import IncomingMessage

logger = logging.getLogger(__name__)


# LLM: Submission preserves Gateway's typed input disposition separately from request execution
# status so transport uncertainty cannot be collapsed into accepted or rejected by an adapter.
# 类用途: 表示一条渠道消息的稳定入口 ID、去向、投递三态和目标活动回合。
@dataclass(frozen=True)
class GatewayAskSubmission:
    request_id: str
    status: str = "queued"
    kind: str = ""
    ok: bool = True
    message: str = ""
    disposition: str = ""
    delivery_status: str = ""
    input_state: str = ""
    target_turn_id: str = ""
    operation_id: str = ""
    receipt_id: str = ""
    control_state: str = ""


# LLM: adapter registry 的健康时间统一使用带时区 UTC ISO，便于跨进程状态投影比较。
# 函数用途: 返回当前 UTC 时间文本。
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_gateway_progress(event: dict[str, object]) -> str:
    if str(event.get("kind") or "") == "assistant_commentary":
        return str(event.get("text") or "").strip()
    tool = str(event.get("tool") or "工具")
    status = str(event.get("status") or "").strip()
    elapsed = event.get("elapsed_seconds")
    elapsed_text = f"（{float(elapsed):.2f} 秒）" if isinstance(elapsed, (int, float)) else ""
    if status == "开始":
        message = f"正在执行：{tool}"
    elif status.startswith("失败"):
        message = f"执行失败：{tool} {status}{elapsed_text}"
    elif status == "完成":
        message = f"执行完成：{tool}{elapsed_text}"
    else:
        message = f"执行进度：{tool} {status}{elapsed_text}".strip()
    detail = str(event.get("detail") or "").strip()
    if detail:
        message += f"\n{detail}"
    if str(event.get("level") or "") == "full":
        output = str(event.get("output") or "").strip()
        if output:
            message += f"\n结果：\n{output}"
    return message


# LLM: Provider idempotency is scoped to one logical channel message, not the whole Gateway request.
# 函数用途: 为同一入站消息的各进度批次和最终回复生成稳定且互不冲突的投递键。
def _gateway_delivery_key(
    *,
    message_id: str,
    request_id: str,
    phase: str,
    progress_cursor: int = 0,
) -> str:
    anchor = str(message_id or request_id or "").strip()
    request = str(request_id or "").strip()
    if not anchor:
        return ""
    if phase == "progress":
        return f"gateway-reply:{anchor}:{request}:progress:{max(0, int(progress_cursor))}"
    return f"gateway-reply:{anchor}:{request}:final"


# LLM: Gateway 回复仍经过唯一 DeliveryService；本 helper 只组装可信入站路由和逻辑消息身份。
# 函数用途: 在不扩张 ChannelManager 职责的前提下投递一条 Gateway 进度或最终回复。
def _deliver_gateway_message(
    delivery_service: DeliveryService,
    *,
    adapter_available: bool,
    msg: IncomingMessage,
    request_id: str,
    response_text: str,
    handle: str = "",
    idempotency_key: str = "",
) -> bool:
    if not adapter_available:
        logger.error(f"找不到 channel={msg.channel} 的适配器")
        return False
    context = DeliveryContext(
        channel=msg.channel,
        target=msg.user_id,
        mode="reply",
        conversation_id=msg.conversation_id,
        reply_to=msg.message_id,
        progress_handle=handle,
        request_id=request_id,
        idempotency_key=(
            str(idempotency_key or "").strip()
            or _gateway_delivery_key(
                message_id=msg.message_id,
                request_id=request_id,
                phase="final",
            )
        ),
    )
    receipt = delivery_service.deliver(context, ReplyEnvelope(content=response_text, format="text"))
    return receipt.delivery_status == "sent"


# LLM: durable pending 记录是后台回复路由的唯一事实，不从响应正文或当前会话状态重建身份。
# 函数用途: 把待回送记录还原为统一的可信入站路由对象。
def _pending_gateway_message(pending: PendingGatewayReply) -> IncomingMessage:
    return IncomingMessage(
        channel=pending.channel,
        user_id=pending.user_id,
        content="",
        message_id=pending.message_id,
        conversation_id=pending.conversation_id,
    )


# LLM: Durable ingress identity is reconstructed only from its typed row. Prepared Gateway content
# may replace original content after media resolution, but route and provider metadata never change.
# 函数用途: 把持久入站记录还原为通道消息，供媒体、Gateway 和控制回复回调复用。
def _gateway_ingress_message(
    record: GatewayIngressRecord,
    *,
    prepared_content: bool = False,
) -> IncomingMessage:
    content = record.content
    if prepared_content and isinstance(record.gateway_payload, dict):
        content = str(record.gateway_payload.get("prompt") or "")
    return IncomingMessage(
        channel=record.channel,
        user_id=record.user_id,
        content=content,
        message_id=record.provider_message_id,
        conversation_id=record.conversation_id,
        timestamp=record.timestamp,
        metadata=dict(record.metadata),
    )


# LLM: Receipt authorization must reuse the exact structured group identity included in the
# persisted Gateway payload. Original provider metadata is only a legacy recovery fallback.
# 函数用途: 从持久入站记录取出 Gateway 已接收的群聊类型和群聊编号，供控制回执继续鉴权。
def _gateway_ingress_channel_identity(
    record: GatewayIngressRecord,
) -> tuple[str, str]:
    payload_metadata: dict[str, object] = {}
    if isinstance(record.gateway_payload, dict):
        candidate = record.gateway_payload.get("metadata")
        if isinstance(candidate, dict):
            payload_metadata = candidate
    original_metadata = record.metadata if isinstance(record.metadata, dict) else {}
    chat_type = str(
        payload_metadata.get("channel_chat_type")
        or original_metadata.get("channel_chat_type")
        or original_metadata.get("feishu_chat_type")
        or original_metadata.get("chat_type")
        or ""
    ).strip().lower()
    chat_id = str(
        payload_metadata.get("channel_chat_id")
        or original_metadata.get("channel_chat_id")
        or original_metadata.get("feishu_chat_id")
        or original_metadata.get("chat_id")
        or ""
    ).strip()
    return chat_type, chat_id


# LLM: Gateway response fields remain typed through the durable ingress row; missing or unknown
# values are validated by the existing watcher selector rather than inferred from display text.
# 函数用途: 从已持久化的 Gateway 响应恢复提交结果对象。
def _gateway_ingress_submission(record: GatewayIngressRecord) -> GatewayAskSubmission:
    payload = record.submission
    if not isinstance(payload, dict):
        raise ValueError("gateway ingress submission is missing")
    return GatewayAskSubmission(
        request_id=str(payload.get("request_id") or "").strip(),
        status=str(payload.get("status") or "queued").strip().lower(),
        kind=str(payload.get("kind") or "").strip().lower(),
        ok=bool(payload.get("ok", True)),
        message=str(payload.get("message") or ""),
        disposition=str(payload.get("disposition") or "").strip().lower(),
        delivery_status=str(payload.get("delivery_status") or "").strip().lower(),
        input_state=str(payload.get("input_state") or "").strip().lower(),
        target_turn_id=str(payload.get("target_turn_id") or "").strip(),
        operation_id=str(payload.get("operation_id") or "").strip(),
        receipt_id=str(payload.get("receipt_id") or "").strip(),
        control_state=str(payload.get("control_state") or "").strip().lower(),
    )


def _gateway_ask_payload(msg: IncomingMessage) -> dict[str, object]:
    # 真实入站消息始终有 conversation_id；getattr 兼容旧的嵌入调用和轻量测试替身。
    conversation_id = str(getattr(msg, "conversation_id", "") or "").strip()
    incoming_metadata = getattr(msg, "metadata", {}) or {}
    chat_type = str(incoming_metadata.get("feishu_chat_type") or incoming_metadata.get("chat_type") or "").strip()
    chat_id = str(incoming_metadata.get("feishu_chat_id") or incoming_metadata.get("chat_id") or "").strip()
    metadata: dict[str, object] = {
        "channel": msg.channel,
        "user_id": msg.user_id,
        "message_id": msg.message_id,
        "adapter": msg.channel,
        "channel_conversation_id": conversation_id,
    }
    if chat_type:
        metadata["channel_chat_type"] = chat_type
    if chat_id:
        metadata["channel_chat_id"] = chat_id
    payload: dict[str, object] = {
        "kind": "ask",
        "prompt": msg.content,
        "metadata": metadata,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
        payload["channel_conversation_id"] = conversation_id
    return payload


# LLM: Adapter identity headers are sourced only from the trusted incoming message structure.
# 函数用途：为普通请求和控制请求生成同一组 Gateway 身份头。
def _gateway_identity_headers(msg: IncomingMessage) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if msg.user_id:
        headers["X-User-Id"] = str(msg.user_id)
    if msg.channel:
        headers["X-Channel"] = str(msg.channel)
    return headers


# LLM: The initial watcher is selected only from Gateway's structured disposition/state. An ask
# without an input receipt may still explicitly return queued status and therefore watch results.
# 函数用途: 决定待回送记录先对账输入去向，还是直接等待任务结果。
def _gateway_submission_watch_kind(submission: GatewayAskSubmission) -> str:
    status = str(submission.status or "").strip().lower()
    disposition = str(submission.disposition or "").strip().lower()
    input_state = str(submission.input_state or "").strip().lower()
    delivery_status = str(submission.delivery_status or "").strip().lower()
    known_input_states = {
        "pending",
        "active_pending",
        "consumed",
        "terminal_unknown",
        "queued",
    }
    if input_state:
        if input_state not in known_input_states:
            raise ValueError("gateway ask submission has invalid input_state")
        expected_disposition = (
            "queued" if input_state == "queued" else "active_turn_input"
        )
        expected_delivery = (
            "accepted" if input_state in {"queued", "consumed"} else "unknown"
        )
        if disposition and disposition != expected_disposition:
            raise ValueError("gateway ask submission disposition conflicts with input_state")
        if delivery_status and delivery_status != expected_delivery:
            raise ValueError("gateway ask submission delivery_status conflicts with input_state")
        return "request_result" if input_state == "queued" else "input_receipt"
    if disposition == "active_turn_input":
        return "input_receipt"
    if disposition == "queued" or (not disposition and status == "queued"):
        return "request_result"
    raise ValueError(
        "gateway ask submission is missing a supported structured disposition"
    )


# LLM: HTTP status classification is transport policy. Missing/rate-limited/server states wait;
# every other HTTP contract/auth failure is quarantined and never rendered as assistant text.
# 函数用途: 判断 Gateway 轮询错误是否属于可安全继续等待的瞬时状态。
def _gateway_poll_http_wait(status_code: int) -> bool:
    code = int(status_code)
    return code in {404, 408, 425, 429} or 500 <= code < 600


# LLM: Quarantine reasons are structured diagnostics and contain no provider response body.
# 函数用途: 为不可恢复的轮询 HTTP 状态生成稳定隔离原因。
def _gateway_poll_quarantine_reason(endpoint: str, status_code: int) -> str:
    code = int(status_code)
    category = "auth" if code in {401, 403} else "config"
    return f"gateway_{endpoint}_{category}_http_{code}"


# LLM: A control watcher is anchored by the independent operation receipt. The target turn remains
# request_id and must never be reused as the durable polling/deduplication key.
# 函数用途: 校验 Gateway 返回的控制回执身份和阶段，防止把目标回合 ID 当成操作回执 ID。
def _validate_control_receipt_submission(submission: GatewayAskSubmission) -> None:
    operation_id = str(submission.operation_id or "").strip()
    receipt_id = str(submission.receipt_id or "").strip()
    control_state = str(submission.control_state or "").strip().lower()
    if not operation_id or not receipt_id:
        raise ValueError("gateway control receipt is missing operation_id or receipt_id")
    if operation_id != receipt_id:
        raise ValueError("gateway control receipt operation_id conflicts with receipt_id")
    if operation_id == str(submission.request_id or "").strip():
        raise ValueError("gateway control receipt reuses target request_id")
    if control_state not in {
        "prepared",
        "executing",
        "completed",
        "terminal_unknown",
    }:
        raise ValueError("gateway control receipt has invalid control_state")


# LLM: Any control operation whose transport/effect boundary remains unknown must enter the durable
# operation receipt watcher. Command kind changes closeout behavior but not stable identity.
# 函数用途: 判断控制提交是否需要创建占位并通过 operation ID 后续对账。
def _control_submission_needs_watcher(submission: GatewayAskSubmission) -> bool:
    return submission.delivery_status == "unknown" or submission.control_state in {
        "prepared",
        "executing",
        "terminal_unknown",
    }


# LLM: This collaborator adapts ChannelManager IO primitives to the durable ingress worker's four
# stage callbacks; it never owns state or starts a second execution path.
# 类用途: 把媒体、Gateway、控制回复和 watcher 移交组装成入站 worker 可调用的阶段动作。
class _ChannelIngressCoordinator:
    def __init__(self, manager: ChannelManager) -> None:
        self._manager = manager

    # LLM: Media provider IO runs only after the prepared row is durable. The resulting exact
    # Gateway body is returned to the worker for CAS persistence before POST.
    # 函数用途: 恢复原始通道消息、下载媒体并生成待提交的 Gateway 请求体。
    def prepare_payload(
        self,
        record: GatewayIngressRecord,
    ) -> dict[str, object]:
        msg = _gateway_ingress_message(record)
        self._manager._maybe_download_media(msg, retry_on_error=True)
        return _gateway_ask_payload(msg)

    # LLM: POST uses content recovered from the persisted prepared payload. If the response is lost,
    # the ingress row remains payload_ready and retries the same stable provider message body.
    # 函数用途: 向 Gateway 提交已持久化的精确请求体，并返回可落盘的结构化回执。
    def submit_payload(
        self,
        record: GatewayIngressRecord,
    ) -> dict[str, object]:
        if not isinstance(record.gateway_payload, dict):
            raise ValueError("gateway ingress payload is missing")
        submission = self._manager._submit_gateway_payload(
            record.gateway_payload,
            _gateway_ingress_message(record, prepared_content=True),
        )
        return asdict(submission)

    # LLM: Control delivery and placeholder creation happen outside ingress store locks. Ordinary
    # messages validate structured disposition before any provider placeholder side effect.
    # 函数用途: 处理 Gateway 回执；控制命令直接收口，普通消息创建占位后等待 watcher 移交。
    def advance_submission(
        self,
        record: GatewayIngressRecord,
    ) -> GatewayIngressAdvance:
        submission = _gateway_ingress_submission(record)
        msg = _gateway_ingress_message(record)
        request_id = submission.request_id
        if submission.status == "control":
            request_id = request_id or f"control-{record.provider_message_id}"
            if _control_submission_needs_watcher(submission):
                _validate_control_receipt_submission(submission)
                adapter = self._manager._adapters.get(record.channel)
                if adapter is None:
                    raise RuntimeError(
                        f"adapter unavailable for channel={record.channel}"
                    )
                handle = adapter.send_progress_placeholder(
                    record.user_id,
                    record.provider_message_id,
                )
                return GatewayIngressAdvance(
                    "placeholder_ready",
                    progress_handle=handle,
                )
            if submission.kind == "stop" and submission.ok:
                self._manager._discard_interrupted_reply(
                    msg,
                    submission.request_id,
                )
                return GatewayIngressAdvance("completed")
            reply_id = submission.operation_id or request_id
            sent = self._manager._send_gateway_reply(
                msg,
                reply_id,
                submission.message or "系统命令没有返回结果。",
            )
            if not sent:
                raise RuntimeError("gateway control reply delivery was not accepted")
            return GatewayIngressAdvance("completed")
        if not request_id:
            raise ValueError("gateway ask submission is missing request_id")
        _gateway_submission_watch_kind(submission)
        adapter = self._manager._adapters.get(record.channel)
        if adapter is None:
            raise RuntimeError(f"adapter unavailable for channel={record.channel}")
        handle = adapter.send_progress_placeholder(
            record.user_id,
            record.provider_message_id,
        )
        return GatewayIngressAdvance("placeholder_ready", progress_handle=handle)

    # LLM: PendingGatewayReply remains the post-POST three-state authority. Repeated handoff is
    # put-if-absent; a newly created placeholder is cleared only after the reply store lock releases.
    # 函数用途: 把普通消息移交给 input_receipt/request_result watcher，并清理非权威占位句柄。
    def handoff_reply(self, record: GatewayIngressRecord) -> None:
        submission = _gateway_ingress_submission(record)
        channel_chat_type, channel_chat_id = _gateway_ingress_channel_identity(record)
        if submission.status == "control":
            _validate_control_receipt_submission(submission)
            request_id = submission.request_id or submission.target_turn_id
            pending = PendingGatewayReply(
                request_id=request_id,
                operation_id=submission.operation_id,
                receipt_id=submission.receipt_id,
                control_kind=submission.kind,
                channel=record.channel,
                user_id=record.user_id,
                message_id=record.provider_message_id,
                conversation_id=record.conversation_id,
                channel_chat_type=channel_chat_type,
                channel_chat_id=channel_chat_id,
                progress_handle=record.progress_handle,
                watch_kind="control_receipt",
                created_at=record.created_at,
            )
        else:
            pending = PendingGatewayReply(
                request_id=submission.request_id,
                channel=record.channel,
                user_id=record.user_id,
                message_id=record.provider_message_id,
                conversation_id=record.conversation_id,
                channel_chat_type=channel_chat_type,
                channel_chat_id=channel_chat_id,
                progress_handle=record.progress_handle,
                watch_kind=_gateway_submission_watch_kind(submission),
                created_at=record.created_at,
            )
        current, created = self._manager._reply_delivery.enqueue(pending)
        if created or (
            current is not None
            and current.progress_handle == record.progress_handle
        ):
            return
        adapter = self._manager._adapters.get(record.channel)
        if adapter is not None:
            adapter.clear_progress_placeholder(
                record.user_id,
                record.progress_handle,
            )


# LLM: This client is the only read-only HTTP projection for adapter reply reconciliation. It
# preserves owner headers and returns WAIT for missing or transient server state.
# 类用途: 查询 Gateway 的进度、输入三态和最终结果，不负责提交新任务或发送通道消息。
class _GatewayReplyPollClient:
    def __init__(self, gateway_port: int) -> None:
        self._gateway_port = int(gateway_port)

    # LLM: Progress polling reads events after the durable cursor and returns the next typed cursor;
    # rendering never changes delivery identity.
    # 函数用途: 拉取并渲染一批新的 Gateway 进度事件。
    def poll_progress(self, pending: PendingGatewayReply) -> tuple[list[str], int]:
        import urllib.error
        import urllib.request

        url = (
            f"http://127.0.0.1:{self._gateway_port}/progress/{pending.request_id}"
            f"?since={pending.progress_cursor}"
        )
        headers = {"X-User-Id": pending.user_id, "X-Channel": pending.channel}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if _gateway_poll_http_wait(exc.code):
                return [], pending.progress_cursor
            raise GatewayReplyQuarantineError(
                _gateway_poll_quarantine_reason("progress", exc.code)
            ) from exc
        events = body.get("events") if isinstance(body, dict) else []
        messages = [
            rendered
            for event in (events if isinstance(events, list) else [])
            if isinstance(event, dict) and (rendered := _render_gateway_progress(event))
        ]
        return messages, max(pending.progress_cursor, int(body.get("next") or 0))

    # LLM: This GET is a read-only reconciliation of the stable ingress id. HTTP errors bubble to
    # the worker and therefore remain WAIT instead of becoming a user-visible terminal failure.
    # 函数用途: 查询普通消息当前是等待活动回合、已消费、需排队还是终态未知。
    def poll_input_receipt(self, pending: PendingGatewayReply) -> str:
        import urllib.error
        import urllib.parse
        import urllib.request

        request_id = urllib.parse.quote(pending.request_id, safe="")
        url = f"http://127.0.0.1:{self._gateway_port}/input-status/{request_id}"
        req = urllib.request.Request(
            url,
            headers={"X-User-Id": pending.user_id, "X-Channel": pending.channel},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if _gateway_poll_http_wait(exc.code):
                return "pending"
            raise GatewayReplyQuarantineError(
                _gateway_poll_quarantine_reason("input_status", exc.code)
            ) from exc
        state = str(body.get("input_state") or "").strip().lower()
        if state not in {
            "pending",
            "active_pending",
            "consumed",
            "terminal_unknown",
            "queued",
        }:
            raise ValueError("gateway input status response has invalid input_state")
        return state

    # LLM: Control reconciliation addresses the operation id but authenticates with the complete
    # persisted issuer scope before verifying receipt aliases and the target turn.
    # 函数用途: 携带通道、用户、会话和群聊身份查询控制回执，让 unknown 投递可跨重启安全对账。
    def poll_control_receipt(
        self,
        pending: PendingGatewayReply,
    ) -> GatewayControlReceiptResult | None:
        import urllib.error
        import urllib.parse
        import urllib.request

        operation_id = urllib.parse.quote(pending.operation_id, safe="")
        url = f"http://127.0.0.1:{self._gateway_port}/control-status/{operation_id}"
        headers = {
            "X-User-Id": pending.user_id,
            "X-Channel": pending.channel,
            "X-Conversation-Id": pending.conversation_id,
            "X-Channel-Chat-Type": pending.channel_chat_type,
            "X-Channel-Chat-Id": pending.channel_chat_id,
        }
        req = urllib.request.Request(
            url,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if _gateway_poll_http_wait(exc.code):
                return None
            raise GatewayReplyQuarantineError(
                _gateway_poll_quarantine_reason("control_status", exc.code)
            ) from exc
        if not isinstance(body, dict):
            raise GatewayReplyQuarantineError(
                "gateway_control_status_config_invalid_body"
            )
        operation_result = str(body.get("operation_id") or "").strip()
        receipt_result = str(body.get("receipt_id") or "").strip()
        target_result = str(body.get("request_id") or "").strip()
        if (
            operation_result != pending.operation_id
            or receipt_result != pending.receipt_id
            or (
                pending.request_id
                and target_result
                and target_result != pending.request_id
            )
        ):
            raise GatewayReplyQuarantineError(
                "gateway_control_status_config_identity_conflict"
            )
        try:
            return GatewayControlReceiptResult(
                control_state=str(body.get("control_state") or ""),
                delivery_status=str(body.get("delivery_status") or ""),
                message=str(body.get("message") or ""),
                target_turn_id=target_result,
                control_kind=str(body.get("kind") or ""),
            )
        except ValueError as exc:
            raise GatewayReplyQuarantineError(
                "gateway_control_status_config_invalid_state"
            ) from exc

    # LLM: Result polling never turns transient transport failure or HTTP 5xx into final user text;
    # only a structured 200 result/error is terminal for the current watcher.
    # 函数用途: 查询一次最终结果；未完成或瞬时服务错误返回 None 继续等待。
    def poll_response(
        self,
        pending: PendingGatewayReply,
        interval: float,
    ) -> str | None:
        import urllib.error
        import urllib.request

        try:
            url = f"http://127.0.0.1:{self._gateway_port}/result/{pending.request_id}"
            req = urllib.request.Request(
                url,
                headers={"X-User-Id": pending.user_id, "X-Channel": pending.channel},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if resp.status == 200 and body.get("ok"):
                return body.get("response", "")
            if resp.status == 200 and "error" in body:
                return f"错误: {body.get('error', 'unknown')}"
            time.sleep(interval)
            return None
        except urllib.error.HTTPError as exc:
            if _gateway_poll_http_wait(exc.code):
                time.sleep(interval)
                return None
            raise GatewayReplyQuarantineError(
                _gateway_poll_quarantine_reason("result", exc.code)
            ) from exc
        except Exception:
            time.sleep(interval)
            return None


# LLM: Manager is the composition root for adapters and the single durable ingress/reply worker;
# new channel IO must be injected into that state machine rather than run in callback threads.
# 类用途: 管理通道生命周期，并把外部消息可靠地提交、对账和回送给原用户。
class ChannelManager:
    """管理所有已注册的通道适配器，提供统一的启停和消息路由接口。"""

    def __init__(
        self,
        gateway_port: int = 8420,
        *,
        delivery_state_dir: Path | None = None,
        delivery_poll_interval: float = 1.0,
    ) -> None:
        self._adapters: dict[str, BaseChannelAdapter] = {}
        self._delivery_registry = ChannelAdapterRegistry()
        self._delivery_service = DeliveryService(self._delivery_registry)
        self.gateway_port = gateway_port
        self._session_channel_file: Path | None = None  # 用于持久化活跃通道
        self._lifecycle_started = False
        self._reply_poll_client = _GatewayReplyPollClient(gateway_port)
        self._poll_gateway_once = self._reply_poll_client.poll_response
        self._poll_gateway_progress = self._reply_poll_client.poll_progress
        self._poll_gateway_input_receipt = self._reply_poll_client.poll_input_receipt
        self._poll_gateway_control_receipt = (
            self._reply_poll_client.poll_control_receipt
        )
        self._reply_delivery = GatewayReplyDeliveryWorker(
            GatewayReplyDeliveryStore(delivery_state_dir),
            poll_response=lambda pending: self._poll_gateway_once(pending, interval=0.0),
            deliver_response=self._deliver_gateway_reply,
            poll_progress=self._poll_gateway_progress,
            deliver_progress=self._deliver_gateway_progress,
            receipt_callbacks=GatewayReplyReceiptCallbacks(
                poll_input=self._poll_gateway_input_receipt,
                poll_control=self._poll_gateway_control_receipt,
                clear_placeholder=self._clear_consumed_input_placeholder,
            ),
            poll_interval=delivery_poll_interval,
        )
        self._ingress_callbacks = _ChannelIngressCoordinator(self)
        self._delivery_worker = GatewayAdapterDeliveryWorker(
            GatewayIngressStore(delivery_state_dir),
            reply_worker=self._reply_delivery,
            prepare_payload=self._ingress_callbacks.prepare_payload,
            submit_payload=self._ingress_callbacks.submit_payload,
            advance_submission=self._ingress_callbacks.advance_submission,
            handoff_reply=self._ingress_callbacks.handoff_reply,
            poll_interval=delivery_poll_interval,
        )

    @property
    def session_channel_file(self) -> Path | None:
        """返回活跃通道存储文件路径。"""
        return self._session_channel_file

    @session_channel_file.setter
    def session_channel_file(self, path: Path | None) -> None:
        """设置活跃通道存储文件路径（供测试和外部注入）。"""
        self._session_channel_file = path

    # -------------------------------------------------------------------------
    # 适配器注册
    # -------------------------------------------------------------------------

    def register_adapter(self, adapter: BaseChannelAdapter) -> None:
        """注册一个通道适配器。"""
        name = adapter.adapter_name
        if name in self._adapters:
            logger.warning(f"适配器 {name} 已注册，将被替换")
        self._adapters[name] = adapter
        self._delivery_registry.register_adapter(name, adapter, configured=True)
        logger.info(f"已注册通道适配器: {name}")

    def get_adapter(self, name: str) -> BaseChannelAdapter | None:
        """获取指定名称的适配器。"""
        return self._adapters.get(name)

    def list_adapters(self) -> list[str]:
        """列出所有已注册的适配器名称。"""
        return list(self._adapters.keys())

    # -------------------------------------------------------------------------
    # 启停
    # -------------------------------------------------------------------------

    # LLM: Adapters start before durable recovery so media and channel callbacks have a live provider;
    # only the combined delivery worker is started, never the reply worker's own thread.
    # 函数用途: 启动所有通道后恢复未完成的入站提交和结果回送。
    def start_all(self) -> None:
        """启动所有已注册的适配器。"""
        for adapter in self._adapters.values():
            if adapter.running:
                self._delivery_registry.mark_health(
                    adapter.adapter_name,
                    "healthy",
                    checked_at=_utc_now_iso(),
                )
                continue
            self._delivery_registry.mark_health(
                adapter.adapter_name,
                "starting",
                checked_at=_utc_now_iso(),
            )
            try:
                adapter.start()
            except Exception as exc:
                self._delivery_registry.mark_health(
                    adapter.adapter_name,
                    "unhealthy",
                    checked_at=_utc_now_iso(),
                    error_code="CHANNEL_ADAPTER_START_FAILED",
                )
                logger.error(f"启动适配器 {adapter.adapter_name} 失败: {exc}")
                continue
            self._delivery_registry.mark_health(
                adapter.adapter_name,
                "healthy" if adapter.running else "unhealthy",
                checked_at=_utc_now_iso(),
                error_code="" if adapter.running else "CHANNEL_ADAPTER_NOT_RUNNING",
            )
        # adapter.start 期间仍属于启动阶段；全部尝试完成后才允许新消息主动唤醒 worker。
        self._lifecycle_started = True
        # 持久化 outbox 必须在生命周期内自动恢复；无状态测试没有记录时不白起线程。
        if (
            self._delivery_worker.store.durable
            or self._reply_delivery.store.durable
            or self._delivery_worker.store.pending()
            or self._reply_delivery.store.pending()
        ):
            self._delivery_worker.start()

    # LLM: Stop joins the only adapter delivery thread before provider shutdown and leaves durable
    # rows untouched for the next process.
    # 函数用途: 停止恢复线程和所有已注册通道，不删除未完成工作。
    def stop_all(self) -> None:
        """停止所有已注册的适配器。"""
        self._lifecycle_started = False
        # 先停唯一投递线程，再断开通道；未完成记录仍在磁盘，下次 start_all 会继续。
        self._delivery_worker.stop()
        for adapter in self._adapters.values():
            if not adapter.running:
                continue
            try:
                adapter.stop()
            except Exception as exc:
                self._delivery_registry.mark_health(
                    adapter.adapter_name,
                    "unhealthy",
                    checked_at=_utc_now_iso(),
                    error_code="CHANNEL_ADAPTER_STOP_FAILED",
                )
                logger.error(f"停止适配器 {adapter.adapter_name} 失败: {exc}")
                continue
            self._delivery_registry.mark_health(
                adapter.adapter_name,
                "stopped",
                checked_at=_utc_now_iso(),
            )

    # LLM: adapter 进程状态文件只能序列化 registry 的脱敏快照，不复制另一套状态判断。
    # 函数用途: 返回所有已注册通道的生命周期和能力状态，供跨进程能力诊断读取。
    def runtime_channel_statuses(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self._delivery_registry.runtime_snapshot()]

    # -------------------------------------------------------------------------
    # 消息路由
    # -------------------------------------------------------------------------

    # LLM: Every authenticated message is durably recorded before media, Gateway POST, placeholder,
    # or reply IO. Same-id/same-body reuses the row; same-id/different-body is quarantined.
    # 函数用途: 只登记外部消息并唤醒后台 worker，不在飞书/WS 回调线程访问 Gateway 或通道接口。
    def route_message(self, msg: IncomingMessage) -> bool:
        try:
            if self._adapters.get(msg.channel) is None:
                logger.error(f"找不到 channel={msg.channel} 的适配器")
                return False
            record = build_gateway_ingress_record(
                channel=msg.channel,
                user_id=msg.user_id,
                conversation_id=str(
                    getattr(msg, "conversation_id", "") or ""
                ).strip(),
                provider_message_id=msg.message_id,
                content=msg.content,
                metadata=dict(getattr(msg, "metadata", {}) or {}),
                timestamp=float(getattr(msg, "timestamp", 0.0) or 0.0),
            )
            _current, _created, quarantined = self._delivery_worker.enqueue(record)
            if quarantined:
                logger.error(
                    "gateway ingress replay quarantined channel=%s provider_message_id=%s",
                    msg.channel,
                    msg.message_id,
                )
                return False
            if self._lifecycle_started:
                self._delivery_worker.start()
            return True
        except Exception as exc:
            logger.error(f"route_message 异常: {exc}")
            return False

    # LLM: Match both request and trusted channel scope before suppressing a late reply.
    # 函数用途: `/stop` 成功后丢弃当前会话该请求的旧回复并撤掉占位提示。
    def _discard_interrupted_reply(self, msg: IncomingMessage, request_id: str) -> None:
        records = self._reply_delivery.discard_where(
            lambda record: (
                record.request_id == request_id
                and record.channel == msg.channel
                and record.user_id == msg.user_id
                and record.conversation_id == msg.conversation_id
            ),
            reason="conversation_user_stop",
        )
        adapter = self._adapters.get(msg.channel)
        if adapter is None:
            return
        for record in records:
            adapter.clear_progress_placeholder(record.user_id, record.progress_handle)

    # LLM: Durable ingress asks provider failures to bubble into worker backoff before POST; direct
    # legacy callers may retain best-effort behavior through the default flag.
    # 函数用途: 下载入站媒体并注入本地路径；worker 模式失败会重试，直接调用默认只记录告警。
    def _maybe_download_media(
        self,
        msg: IncomingMessage,
        *,
        retry_on_error: bool = False,
    ) -> None:
        """入站图片/文件下载到 adapter 工作区,content 注入绝对路径(agent 可据此 analyze_image/读文件)。
        失败静默(不影响消息处理);无 media / 通道不支持媒体直接跳过。"""
        media = (msg.metadata or {}).get("media")
        adapter = self._adapters.get(msg.channel)
        root = getattr(adapter, "workspace_root", None)
        if not media or root is None or not hasattr(adapter, "fetch_media_to"):
            return
        try:
            dest = Path(root) / "inbound_media"
            name = adapter.fetch_media_to(msg.message_id, media, dest)
            if name:
                msg.content = f"{msg.content}\n[已下载到: {dest / name}]"
        except Exception as exc:
            suffix = "，等待 durable worker 重试" if retry_on_error else "(不影响处理)"
            logger.warning(f"入站媒体下载失败{suffix}: {exc}")
            if retry_on_error:
                raise

    # LLM: The adapter copies Gateway's typed routing fields without inferring acceptance from
    # HTTP success or the human message. Missing fields remain empty for explicit validation.
    # 函数用途: 为直接调用者构造 Gateway 请求体，再复用精确 payload 提交入口。
    def _submit_gateway_ask(self, msg: IncomingMessage) -> GatewayAskSubmission:
        return self._submit_gateway_payload(_gateway_ask_payload(msg), msg)

    # LLM: Durable ingress passes the exact gateway_payload already persisted before POST; this
    # method must never rebuild or enrich it from mutable message content.
    # 函数用途: 原样发送已持久化的 Gateway 请求体，并解析结构化输入去向回执。
    def _submit_gateway_payload(
        self,
        payload: dict[str, object],
        msg: IncomingMessage,
    ) -> GatewayAskSubmission:
        import urllib.request

        # 转发真实渠道身份(走 127.0.0.1 回环=网关可信来源):渠道用户拿到自己的身份/USER 角色,
        # 不再因"缺头"被当本机终端 admin(审计 #2:渠道用户全跑成 admin)。
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.gateway_port}/ask",
            data=json.dumps(payload).encode("utf-8"),
            headers=_gateway_identity_headers(msg),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8", "replace"))
        request_id = str(result.get("request_id") or "").strip()
        status = str(result.get("status") or "queued").strip().lower()
        if not request_id and status != "control":
            logger.error(f"gateway /ask 未返回 request_id: {result}")
        return GatewayAskSubmission(
            request_id=request_id,
            status=status,
            kind=str(result.get("kind") or "").strip().lower(),
            ok=bool(result.get("ok", True)),
            message=str(result.get("message") or ""),
            disposition=str(result.get("disposition") or "").strip().lower(),
            delivery_status=str(result.get("delivery_status") or "")
            .strip()
            .lower(),
            input_state=str(result.get("input_state") or "").strip().lower(),
            target_turn_id=str(result.get("target_turn_id") or "").strip(),
            operation_id=str(result.get("operation_id") or "").strip(),
            receipt_id=str(result.get("receipt_id") or "").strip(),
            control_state=str(result.get("control_state") or "").strip().lower(),
        )

    # LLM: 最终回复与显式消息共用 DeliveryService；当前 channel/user/reply_to 只能取入站可信结构。
    # 函数用途: 把 Gateway 最终正文装入无收件人的 ReplyEnvelope，并回复原用户。
    def _send_gateway_reply(
        self,
        msg: IncomingMessage,
        request_id: str,
        response_text: str,
        handle: str = "",
        *,
        idempotency_key: str = "",
    ) -> bool:
        sent = _deliver_gateway_message(
            self._delivery_service,
            adapter_available=self._adapters.get(msg.channel) is not None,
            msg=msg,
            request_id=request_id,
            response_text=response_text,
            handle=handle,
            idempotency_key=idempotency_key,
        )
        if sent:
            self._update_active_channel(msg.user_id, msg.channel)
        return sent

    def _deliver_gateway_reply(self, pending: PendingGatewayReply, response_text: str) -> bool:
        return self._send_gateway_reply(
            _pending_gateway_message(pending),
            pending.stable_id,
            response_text,
            pending.progress_handle,
        )

    def _deliver_gateway_progress(self, pending: PendingGatewayReply, response_text: str) -> bool:
        return self._send_gateway_reply(
            _pending_gateway_message(pending),
            pending.request_id,
            response_text,
            idempotency_key=_gateway_delivery_key(
                message_id=pending.message_id,
                request_id=pending.request_id,
                phase="progress",
                progress_cursor=pending.progress_cursor,
            ),
        )

    # LLM: A consumed input belongs to the already-running turn's original reply delivery. This
    # callback removes only the transient placeholder and never emits another chat message.
    # 函数用途: 活动回合已消费补充消息后撤掉本条消息的处理中提示。
    def _clear_consumed_input_placeholder(self, pending: PendingGatewayReply) -> None:
        adapter = self._adapters.get(pending.channel)
        if adapter is None:
            return
        adapter.clear_progress_placeholder(
            pending.user_id,
            pending.progress_handle,
        )

    # -------------------------------------------------------------------------
    # 活跃通道查询
    # -------------------------------------------------------------------------

    def get_active_channel(self, user_id: str) -> str | None:
        """查询用户当前活跃的通道。"""
        if self._session_channel_file is None:
            return None
        try:
            if not self._session_channel_file.exists():
                return None
            data = json.loads(self._session_channel_file.read_text(encoding="utf-8"))
            return data.get(user_id)
        except (json.JSONDecodeError, OSError):
            return None

    def _update_active_channel(self, user_id: str, channel: str) -> None:
        """更新用户当前活跃通道到本地文件。"""

        if self._session_channel_file is None:
            return
        try:
            data = {}
            if self._session_channel_file.exists():
                data = json.loads(self._session_channel_file.read_text(encoding="utf-8"))
            data[user_id] = channel
            self._session_channel_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_channel_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"更新活跃通道失败: {exc}")

    def update_active_channel(self, user_id: str, channel: str) -> None:
        """公开的更新活跃通道方法。"""
        self._update_active_channel(user_id, channel)
