# LLM: 通道仍沿原 durable worker，G3 凭据只在原 HTTP 身份头附加；读取失败按 G2b 开关处理——开关关（默认）
#   降级为不带凭据继续发送，开关开才拒绝。秘密不进记录、日志或模型。
# 模块用途: 管理 IM 提交和对账；本机凭据不可用时按开关降级或安全拒绝，敏感命令一次性提交不落盘。
from __future__ import annotations

"""LLM: ChannelManager persists authenticated adapter ingress before any Gateway or provider IO.
The only exception is a catalog-declared sensitive_input command (may carry the admin password):
it is never persisted and is submitted once directly.

模块用途: 注册外部通道，并把入站消息交给可恢复的单线程投递状态机处理；可能带管理员密码的命令不落盘、直接提交一次。
"""
# G3：提交和四条对账入口都在原身份头处加同一个宿主凭据；凭据不进入持久入站/回复记录。

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..command_catalog import sensitive_command_name
from ..conversation.host_notices import with_host_notice_lines
from ..delivery import ChannelAdapterRegistry, DeliveryContext, DeliveryService, ReplyEnvelope
from ..gateway_parts.client_credentials import GatewayClientCredentials, gateway_client_credentials
from ..gateway_parts.local_client_token import LocalClientCredentialError
from ..user_space.home_layout import home_paths
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
_SENSITIVE_COMMAND_UNAVAILABLE = "服务暂时不可用，命令没有执行，请稍后重试。"


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


# LLM: 只渲染 Gateway /progress 的结构化事件；permission_requested 只提示可发送的控制命令，既不代表批准，
#   也不是最终回复（最终正文仍由原请求结果 watcher 发送）；提示只含工具名与 Gateway 已脱敏的摘要。
# 函数用途: 把一条过程事件渲染成 IM 可读文本；工具等待确认时告诉用户用 /approve 或 /deny。
def _render_gateway_progress(event: dict[str, object]) -> str:
    kind = str(event.get("kind") or "")
    if kind == "assistant_commentary":
        return str(event.get("text") or "").strip()
    if kind == "permission_requested":
        summary = str(event.get("summary") or event.get("tool") or "一个工具操作").strip()
        return f"代理请求：{summary}。回复 /approve <管理员密码> 允许本次，/deny 拒绝。"
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


# LLM: 身份只取可信入站结构，凭据只取宿主 G1/配置；凭据失败时开关开抛 G1 原因码、开关关降级省略头
#   （由 G2a 计数兜底）。不能落盘秘密，也不静默改写身份头。
# 函数用途: 为普通和控制提交生成原身份头及唯一的 Gateway token。
def _gateway_identity_headers(msg: IncomingMessage, credentials: GatewayClientCredentials) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if msg.user_id:
        headers["X-User-Id"] = str(msg.user_id)
    if msg.channel:
        headers["X-Channel"] = str(msg.channel)
    headers.update(credentials.headers())
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


# LLM: 只适配原 durable worker 四阶段，G3 凭据故障返回结构化拒绝以收口，不重试未发送的 POST 或新建状态机。
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

    # LLM: 原持久正文提交合同不变；G3 凭据失败落安全拒绝码而非网络重试，秘密不进持久行。
    # 函数用途: 提交精确正文；本机凭据失败交给原 worker 收口并回复用户，不重试 POST。
    def submit_payload(
        self,
        record: GatewayIngressRecord,
    ) -> dict[str, object]:
        if not isinstance(record.gateway_payload, dict):
            raise ValueError("gateway ingress payload is missing")
        try:
            submission = self._manager._submit_gateway_payload(
                record.gateway_payload,
                _gateway_ingress_message(record, prepared_content=True),
            )
        except LocalClientCredentialError as exc:
            return {"ok": False, "kind": "credential_error", "error_code": exc.reason_code, "message": str(exc)}
        return asdict(submission)

    # LLM: 原提交回执保留；本机凭据失败用结构化 kind 收口，不创建回复 watcher、不重放；只发安全原因。
    # 函数用途: 处理原回执或本机拒绝；只对已经发送的普通消息创建占位和 watcher。
    def advance_submission(
        self,
        record: GatewayIngressRecord,
    ) -> GatewayIngressAdvance:
        if isinstance(record.submission, dict) and record.submission.get("kind") == "credential_error":
            if not self._manager._send_gateway_reply(_gateway_ingress_message(record), record.ingress_id, record.submission["message"]):
                raise RuntimeError("gateway credential error delivery was not accepted")
            return GatewayIngressAdvance("completed")
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


# LLM: 启动前用原 Agent 数据根与部署 token 同次绑定提交及对账；开关开且凭据不可读时拒绝启动，
#   开关关时记一次降级 warning 后继续启动。
# 函数用途: 为适配器原运输设置唯一宿主凭据来源，不写日志、环境或状态文件。
def configure_gateway_client(manager: ChannelManager, agent: object) -> None:
    credentials = gateway_client_credentials(agent)
    credentials.headers()
    manager._reply_poll_client.credentials = credentials


# LLM: 敏感提交不落盘、不重试；G2b 开关打开时凭据拒绝只返回安全原因，网络错误只记类型，不把密码或 token 带入日志。
# 函数用途: 执行一次敏感控制提交，给原通道投递返回安全文案和编号。
def _sensitive_gateway_submission(manager: ChannelManager, msg: IncomingMessage) -> tuple[str, str]:
    reply_id = f"control-{msg.message_id}"
    try:
        submission = manager._submit_gateway_ask(msg)
        return submission.operation_id or reply_id, submission.message or "系统命令没有返回结果。"
    except LocalClientCredentialError as exc:
        return reply_id, str(exc)
    except Exception as exc:
        logger.warning("敏感命令提交失败 channel=%s error_type=%s", msg.channel, type(exc).__name__)
        return reply_id, _SENSITIVE_COMMAND_UNAVAILABLE


# LLM: IM 对账仍只有原只读 HTTP 入口；各原身份头附同一宿主凭据。读取失败不被吞成网络 WAIT：
#   开关开抛 G1 原因码，开关关降级为不带凭据（由 G2a 计数兜底）。
# 类用途: 带凭据查询进度、输入、控制及结果；不提交新任务，不持久化秘密。
class _GatewayReplyPollClient:
    # LLM: 构造仅选路径，不读秘密；CLI 在启动前以原 Agent 数据根和配置 token 替换来源。
    # 函数用途: 绑定原 Gateway 端口和进程内凭据来源，不发请求。
    def __init__(self, gateway_port: int) -> None:
        self._gateway_port = int(gateway_port)
        self.credentials = GatewayClientCredentials(home_paths().root)

    # LLM: 原游标/身份保持，凭据在运输 try 之前读取；开关开时凭据异常不混成远端瞬时错误，开关关时降级发送。
    # 函数用途: 带宿主凭据拉取并渲染 Gateway 进度，不写秘密。
    def poll_progress(self, pending: PendingGatewayReply) -> tuple[list[str], int]:
        import urllib.error
        import urllib.request

        url = (
            f"http://127.0.0.1:{self._gateway_port}/progress/{pending.request_id}"
            f"?since={pending.progress_cursor}"
        )
        headers = {"X-User-Id": pending.user_id, "X-Channel": pending.channel}
        headers.update(self.credentials.headers())
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

    # LLM: 稳定输入编号仍只读对账；原身份头加凭据。开关开时读取失败在请求前抛 G1 原因码，
    #   开关关时降级为不带凭据；远端瞬时状态仍等待。
    # 函数用途: 带凭据查询普通消息投递状态，不重复提交。
    def poll_input_receipt(self, pending: PendingGatewayReply) -> str:
        import urllib.error
        import urllib.parse
        import urllib.request

        request_id = urllib.parse.quote(pending.request_id, safe="")
        url = f"http://127.0.0.1:{self._gateway_port}/input-status/{request_id}"
        req = urllib.request.Request(
            url,
            headers={"X-User-Id": pending.user_id, "X-Channel": pending.channel, **self.credentials.headers()},
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

    # LLM: 控制仍按 operation ID 和完整签发范围对账；凭据在同一身份头入口添加，不改别名/目标核对合同。
    #   开关开时读取失败不发请求，开关关时降级发送。
    # 函数用途: 带宿主凭据及原通道/会话/群聊身份读取控制回执。
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
        headers.update(self.credentials.headers())
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

    # LLM: 凭据读取在广义运输异常捕获之外：开关开时失败明确抛原因码，开关关时降级为不带凭据；
    #   真正的网络/5xx 仍沿原等待语义。
    # 函数用途: 带宿主凭据查最终结果，秘密不进回复或日志，未完成继续等待。
    def poll_response(
        self,
        pending: PendingGatewayReply,
        interval: float,
    ) -> str | None:
        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{self._gateway_port}/result/{pending.request_id}"
        req = urllib.request.Request(
            url,
            headers={"X-User-Id": pending.user_id, "X-Channel": pending.channel, **self.credentials.headers()},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if resp.status == 200 and body.get("ok"):
                return _reply_text_with_host_notices(body)
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


# LLM: 宿主提示只从结果的 channel_delivery.host_notices 结构化字段取，渲染在同一条回复的正文前；没有提示或正文不是文本时
#   原样返回。渲染后的整段正文进入适配层原有的持久回复记录，重试时一并重发。
# 函数用途: 组装 IM 最终回复正文：宿主提示在前、模型回复在后。
def _reply_text_with_host_notices(body: dict) -> object:
    delivery = body.get("channel_delivery") if isinstance(body.get("channel_delivery"), dict) else {}
    content = body.get("response", "")
    return with_host_notice_lines(content, delivery.get("host_notices")) if isinstance(content, str) else content


# LLM: IM 仍沿唯一入站/回复 worker；G3 凭据只在运输头使用，配置来源从 CLI Agent 绑定，不落到记录或模型上下文。
# 类用途: 管理通道并以宿主凭据提交和对账；原用户身份和持久投递状态不变。
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
    # 例外：命令目录声明 sensitive_input 的命令（/admin、/approve、/deny）可能带管理员密码，绝不进入持久入站队列，
    # 改由 _route_sensitive_command 一次性直接提交。
    # 函数用途: 只登记外部消息并唤醒后台 worker，不在飞书/WS 回调线程访问 Gateway 或通道接口。
    def route_message(self, msg: IncomingMessage) -> bool:
        try:
            if self._adapters.get(msg.channel) is None:
                logger.error(f"找不到 channel={msg.channel} 的适配器")
                return False
            if sensitive_command_name(msg.content):
                return self._route_sensitive_command(msg)
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

    # LLM: 敏感命令不写入口记录、回复 watcher 或占位句柄，也不重试；回调线程只启动一个短生命周期线程后立即返回，
    #   这条线程不持有任何持久状态，不是第二条投递状态机。
    # 函数用途: 把可能带管理员密码的命令交给一次性后台线程直接提交。
    def _route_sensitive_command(self, msg: IncomingMessage) -> bool:
        threading.Thread(
            target=self._deliver_sensitive_command,
            args=(msg,),
            name="gateway-sensitive-command",
            daemon=True,
        ).start()
        return True

    # LLM: 敏感命令只提交一次；本机凭据失败给安全原因码和宿主处置提示，网络错误仍用固定不可用文案；不记录密码/秘密。
    # 函数用途: 直接提交敏感控制并回复原私聊，凭据失败不请求、不持久化、不重试。
    def _deliver_sensitive_command(self, msg: IncomingMessage) -> None:
        reply_id, text = _sensitive_gateway_submission(self, msg)
        try:
            self._send_gateway_reply(msg, reply_id, text)
        except Exception as exc:
            logger.warning("敏感命令回复发送失败 channel=%s error_type=%s", msg.channel, type(exc).__name__)

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

    # LLM: 原持久正文不变；凭据仅在原身份头附加，失败按 G2b 开关降级或拒绝；不把秘密写入入站/回复记录或日志。
    # 函数用途: 带宿主凭据原样提交 Gateway 正文，再解析原结构化回执。
    def _submit_gateway_payload(
        self,
        payload: dict[str, object],
        msg: IncomingMessage,
    ) -> GatewayAskSubmission:
        import urllib.request

        # 渠道身份保持不变；迁移期虽仍信任回环，客户端已经按后续强制合同带宿主凭据。
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.gateway_port}/ask",
            data=json.dumps(payload).encode("utf-8"),
            headers=_gateway_identity_headers(msg, self._reply_poll_client.credentials),
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
