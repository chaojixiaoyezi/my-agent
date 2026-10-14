from __future__ import annotations

"""LLM: 所有外部最终回复、主动消息和附件都必须经过本服务，禁止业务层直接调用 provider API。

模块用途: 组合可信路由与无收件人信封，首发前安全冻结所有附件字节，净化正文并调用已注册 IM adapter。
"""

import hashlib
import logging
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..conversation.channels import (
    ChannelAttachment,
    DeliveryContext,
    DeliveryReceipt,
    ReplyEnvelope,
    leads_with_internal_signal,
    project_host_paths_for_channel,
    project_message_paths_for_channel,
    project_user_reply,
    redact_delivery_context_identifiers,
    redact_host_absolute_paths,
    supports_transcript_delivery,
)
from .attachment_content import VerifiedAttachmentContent, verify_attachment_content
from .registry import ChannelAdapterRegistry, ChannelCapabilities

_LOGGER = logging.getLogger(__name__)
_DELIVERY_MODES = frozenset({"reply", "proactive"})
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


# LLM: 发送失败记录不含完整收件地址，且按 channel/target/error 去重避免故障风暴淹没日志。
# 类用途: 保存一次投递失败的稳定诊断字段。
@dataclass(frozen=True)
class _DeliveryFailure:
    channel: str
    target: str
    error_code: str
    message: str
    detail: str = ""


# LLM: attempt 只在本次发送内持有已核 owner/hash/size 的不可变 bytes；不持久化或由模型字段恢复内容。
# 类用途: 保存通过前置校验的路由/正文和冻结附件，后续上传不重读可被替换的文件路径。
@dataclass(frozen=True)
class _DeliveryAttempt:
    adapter: Any
    capabilities: ChannelCapabilities
    context: DeliveryContext
    envelope: ReplyEnvelope
    content: str
    projection_status: str
    attachments: tuple[VerifiedAttachmentContent, ...] = ()


# LLM: 前置净化结果把规范化路由、用户正文、能力和基础回执冻结，后续解析 adapter 不再重复计算。
# 类用途: 保存通过模式、内部协议和能力检查后的待解析投递。
@dataclass(frozen=True)
class _PreparedDelivery:
    receipt: DeliveryReceipt
    channel: str
    target: str
    content: str
    projection_status: str
    capabilities: ChannelCapabilities


# LLM: DeliveryService 是唯一外部发送 chokepoint；路由、净化、能力校验和 provider 调用不得在上层复制。
# 类用途: 通过注册表把平台无关回复投递到可信上下文指定的 IM 目标。
class DeliveryService:
    # LLM: service 只持有当前运行边界的 registry 和日志去重状态，不保存模型会话或用户正文历史。
    # 函数用途: 创建统一投递服务。
    def __init__(self, registry: ChannelAdapterRegistry) -> None:
        self.registry = registry
        self._reported_failures: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    # LLM: 主动外呼能力只认 registry 声明，后台会话路由不得维护第二份 provider 名单。
    # 函数用途: 判断已注册通道是否允许 proactive 投递。
    def supports_proactive(self, channel: str) -> bool:
        return self.registry.capabilities_for(channel).proactive

    # LLM: 声明与可用必须分开暴露：obligation/route 归属读声明，真实发送读可用性。
    # 否则 adapter 掉线或凭据缺失就会被误判成"不欠外发"，通知被静默吞掉。
    # 函数用途: 判断部署是否声明过该通道（与 adapter 当前是否可用无关）。
    def declares_channel(self, channel: str) -> bool:
        probe = getattr(self.registry, "declares_channel", None)
        if callable(probe):
            return bool(probe(channel))
        return bool(self.registry.capabilities_for(channel).text)

    # LLM: 声明能力同样与可用性无关；只有声明为 proactive 的通道才可能欠真实外发。
    # 函数用途: 返回该通道声明的 proactive 能力（不探测 adapter 是否在线）。
    def declared_proactive(self, channel: str) -> bool:
        return bool(self.registry.capabilities_for(channel).proactive)

    # LLM: provider registry 只管外部 adapter；CLI/Gateway 的本地交付能力由会话协议
    # 统一声明，禁止把未知外部通道错降级成本地 transcript。
    # 函数用途: 判断当前通道能否以权威会话追加作为交付提交。
    def supports_transcript(self, channel: str) -> bool:
        return supports_transcript_delivery(channel)

    # LLM: 卡片只承运宿主引用，仍经统一目标与能力前检；不将普通消息或模型正文当作审批。
    # 函数用途: 为原审批或私密输入流程发送原生卡片并返回真实消息身份。
    def deliver_control_card(self, context: DeliveryContext, card: dict, *, summary: str):
        from .control_cards import deliver_control_card

        return deliver_control_card(self, context, card, summary=summary)

    # LLM: 进度和最终回复共用相同路由/净化/能力前置门；message_id 只可来自 pending 回执，编辑失败不新发替代消息。
    # 函数用途: 首次创建一条进度消息，以后编辑同一条；进度不附路径说明，返回 provider 消息 ID 供原投递账本耐久保存。
    def deliver_progress(self, context: DeliveryContext, envelope: ReplyEnvelope, *, message_id: str = ""):
        from .progress import ProgressDeliveryReceipt

        prepared = self._prepare(context, envelope, progress=True)
        if isinstance(prepared, DeliveryReceipt):
            return ProgressDeliveryReceipt(False, message_id, prepared.error_code or "CHANNEL_PROGRESS_REJECTED")
        attempt = self._resolve_attempt(context, envelope, prepared)
        if isinstance(attempt, DeliveryReceipt):
            return ProgressDeliveryReceipt(False, message_id, attempt.error_code)
        if not attempt.capabilities.edit_progress:
            return ProgressDeliveryReceipt(False, message_id, "CHANNEL_PROGRESS_UNSUPPORTED")
        try:
            text = attempt.content[-8000:]
            if message_id:
                ok = attempt.adapter.edit_progress_message(message_id, text)
                return ProgressDeliveryReceipt(bool(ok), message_id, "" if ok else "CHANNEL_PROGRESS_EDIT_FAILED")
            identity = attempt.adapter.start_progress_message(context.target, text,
                reply_to=context.reply_to, idempotency_key=context.idempotency_key)
            return ProgressDeliveryReceipt(bool(identity), str(identity or ""), "" if identity else "CHANNEL_PROGRESS_SEND_FAILED")
        except Exception:
            return ProgressDeliveryReceipt(False, message_id, "CHANNEL_PROGRESS_IO_FAILED")

    # LLM: context 是收件权威、envelope 是内容；任何发送前都校验模式、目标、内部协议和 adapter 能力。
    # 函数用途: 投递一份回复信封并返回不抛异常的结构化回执。
    def deliver(self, context: DeliveryContext, envelope: ReplyEnvelope) -> DeliveryReceipt:
        prepared = self._prepare(context, envelope)
        if isinstance(prepared, DeliveryReceipt):
            return prepared
        attempt = self._resolve_attempt(context, envelope, prepared)
        if isinstance(attempt, DeliveryReceipt):
            return attempt
        status, error_code = self._safe_deliver(attempt)
        return replace(prepared.receipt, delivery_status=status, error_code=error_code)

    # LLM: 只认宿主typed附件并先拒JSON伪造；净化和能力前检不触碰adapter，失败在provider网络副作用前返回。
    # 函数用途: 规范化 context/envelope 并生成待解析投递，进度只做路径净化，最终消息保留说明，或返回无需发送的终态回执。
    def _prepare(
        self, context: DeliveryContext, envelope: ReplyEnvelope, *, progress: bool = False
    ) -> _PreparedDelivery | DeliveryReceipt:
        channel = str(context.channel or "").strip().lower()
        target = str(context.target or "").strip()
        raw_content = str(envelope.content or "")
        attachments = tuple(envelope.attachments or ())
        receipt = _initial_receipt(context, envelope, channel=channel, target=target)
        if any(not isinstance(item, ChannelAttachment) for item in attachments):
            return replace(receipt, delivery_status='rejected', error_code='ARTIFACT_VALIDATION_FAILED')
        if context.mode not in _DELIVERY_MODES:
            return replace(
                receipt, delivery_status="rejected", error_code="CHANNEL_DELIVERY_MODE_INVALID"
            )
        if context.mode == "proactive" and raw_content.strip() and leads_with_internal_signal(raw_content):
            return replace(receipt, delivery_status="suppressed")
        projection = project_user_reply(raw_content)
        content = redact_delivery_context_identifiers(
            (project_host_paths_for_channel if progress else project_message_paths_for_channel)(projection.content, channel),
            context,
        )
        receipt = replace(receipt, content=content)
        if not content.strip() and not attachments:
            return replace(receipt, delivery_status="not_applicable")
        capabilities = self.registry.capabilities_for(channel)
        if context.mode == "proactive" and not capabilities.proactive:
            return replace(
                receipt,
                delivery_status="not_applicable",
                error_code="CHANNEL_PROACTIVE_UNSUPPORTED",
            )
        return _PreparedDelivery(
            receipt=receipt,
            channel=channel,
            target=target,
            content=content,
            projection_status=projection.projection_status,
            capabilities=capabilities,
        )

    # LLM: 目标/adapter只从原registry解析，所有附件在首个副作用前按原owner/hash安全读；未知字节接口不回落Path。
    # 函数用途: 冻结一份可执行投递及完整附件字节，路径/版本/能力失败均在发文字之前拒绝。
    def _resolve_attempt(
        self,
        context: DeliveryContext,
        envelope: ReplyEnvelope,
        prepared: _PreparedDelivery,
    ) -> _DeliveryAttempt | DeliveryReceipt:
        target_decision = self.registry.validate_target(prepared.channel, prepared.target)
        if not target_decision.allowed:
            self._report_once(
                _DeliveryFailure(
                    prepared.channel,
                    prepared.target,
                    target_decision.error_code,
                    f"channel target rejected target_kind={target_decision.target_kind}",
                )
            )
            return replace(
                prepared.receipt,
                delivery_status="rejected",
                error_code=target_decision.error_code,
            )
        adapter = self.registry.adapter_for(prepared.channel)
        if adapter is None:
            self._report_once(
                _DeliveryFailure(
                    prepared.channel,
                    prepared.target,
                    "CHANNEL_ADAPTER_UNAVAILABLE",
                    "adapter unavailable",
                )
            )
            return replace(
                prepared.receipt,
                delivery_status="unavailable",
                error_code="CHANNEL_ADAPTER_UNAVAILABLE",
            )
        if envelope.attachments and (not prepared.capabilities.frozen_attachment_content
                or getattr(adapter, 'supports_frozen_attachment_content', False) is not True
                or not callable(getattr(adapter, 'send_attachment_content', None))):
            return replace(prepared.receipt, delivery_status='rejected', error_code='CHANNEL_ATTACHMENT_CONTENT_UNSUPPORTED')
        try:
            attachments = _verified_delivery_attachments(envelope, prepared.capabilities)
        except (OSError, ValueError, TypeError):
            return replace(prepared.receipt, delivery_status='rejected', error_code='ARTIFACT_VALIDATION_FAILED')
        return _DeliveryAttempt(
            adapter=adapter,
            capabilities=prepared.capabilities,
            context=context,
            envelope=envelope,
            content=prepared.content,
            projection_status=prepared.projection_status,
            attachments=attachments,
        )

    # LLM: 文字/字节均已预检，普通回复仍保留引用/typing，异常日志只类型不放供应商正文或凭据。
    # 函数用途: 调用 adapter 的通用消息和原生附件接口，并把异常收敛为稳定错误码。
    def _safe_deliver(self, attempt: _DeliveryAttempt) -> tuple[str, str]:
        try:
            if attempt.content.strip() and not self._send_text(attempt):
                return "failed", "CHANNEL_SEND_FAILED"
            if not self._send_attachments(attempt):
                return "failed", "CHANNEL_SEND_FAILED"
            _LOGGER.warning(
                "NATIVE_CHANNEL_SEND_OK channel=%s target=***%s content_len=%d attachments=%d mode=%s",
                attempt.context.channel,
                str(attempt.context.target)[-6:],
                len(attempt.content),
                len(attempt.envelope.attachments),
                attempt.context.mode,
            )
            return "sent", ""
        except Exception as exc:
            self._report_once(
                _DeliveryFailure(
                    attempt.context.channel,
                    attempt.context.target,
                    "CHANNEL_SEND_EXCEPTION",
                    "channel send exception",
                    detail=f"type={type(exc).__name__}",
                )
            )
            return "failed", "CHANNEL_SEND_EXCEPTION"

    # LLM: OutgoingMessage metadata 只由可信 context 和净化状态构造，不合并模型提供的任意 metadata。
    # 函数用途: 发送正文，并在 reply 模式收掉 provider 的处理中状态。
    def _send_text(self, attempt: _DeliveryAttempt) -> bool:
        from ..adapter.protocol import OutgoingMessage

        if not attempt.capabilities.text:
            return False
        metadata = {
            "gateway_request_id": attempt.context.request_id,
            "delivery_idempotency_key": (
                attempt.context.idempotency_key
                or attempt.context.request_id
            ),
            "reply_to": attempt.context.reply_to,
            "conversation_id": attempt.context.conversation_id,
            "projection_status": attempt.projection_status,
            "evidence_refs": list(attempt.envelope.evidence_refs),
        }
        outgoing = OutgoingMessage(
            channel=attempt.context.channel,
            user_id=attempt.context.target,
            content=attempt.content,
            format=attempt.envelope.format,
            metadata={key: value for key, value in metadata.items() if value},
        )
        if attempt.context.mode == "reply":
            if not attempt.capabilities.reply:
                return False
            return bool(
                attempt.adapter.finalize_response(
                    attempt.context.target, attempt.context.progress_handle, outgoing
                )
            )
        return bool(attempt.adapter.send_message(attempt.context.target, outgoing))

    # LLM: 附件能力逐项 fail-closed；不能把不支持的附件退化成服务器路径文本或 MEDIA 标记。
    # 函数用途: 按信封顺序发送所有 typed 附件。
    def _send_attachments(self, attempt: _DeliveryAttempt) -> bool:
        for index, attachment in enumerate(attempt.attachments):
            if not self._send_attachment(
                attempt,
                attachment,
                idempotency_key=_attachment_idempotency_key(
                    attempt.context,
                    attachment.attachment,
                    index,
                ),
            ):
                return False
        return True

    # LLM: 统一只传已核验的不可变字节，adapter不能从原path重读；不支持此协议不能降级旧Path接口。
    # 函数用途: 将一项已冻结附件交给原生字节发送接口，保留同一稳定投递键。
    def _send_attachment(
        self,
        attempt: _DeliveryAttempt,
        content: VerifiedAttachmentContent,
        *,
        idempotency_key: str,
    ) -> bool:
        return bool(attempt.adapter.send_attachment_content(
            attempt.context.target, content, idempotency_key=idempotency_key,
            as_image=Path(content.attachment.path).suffix.lower() in _IMAGE_SUFFIXES and attempt.capabilities.images))

    # LLM: 同类故障只打一条脱敏日志，不能记录完整用户 ID、正文、附件路径或凭据。
    # 函数用途: 去重并记录一次投递失败。
    def _report_once(self, failure: _DeliveryFailure) -> None:
        key = (failure.channel, failure.target, failure.error_code)
        with self._lock:
            if key in self._reported_failures:
                return
            if len(self._reported_failures) >= 2048:
                self._reported_failures.pop()
            self._reported_failures.add(key)
        _LOGGER.warning(
            "channel delivery failure channel=%s target_len=%d error_code=%s message=%s detail=%s",
            failure.channel,
            len(failure.target),
            failure.error_code,
            failure.message,
            failure.detail,
        )


# LLM: envelope只携带原引用，不信任serialized内容；能力决定原size上限，读取/核验完成前不发任何正文或附件。
# 函数用途: 按原图片/文件选择规则冻结完整集合，任一失败不返回部分内容。
def _verified_delivery_attachments(envelope: ReplyEnvelope, capabilities: ChannelCapabilities) -> tuple[VerifiedAttachmentContent, ...]:
    prepared = []
    for attachment in envelope.attachments:
        if not isinstance(attachment, ChannelAttachment):
            raise ValueError('附件必须来自宿主引用投影')
        image = Path(attachment.path).suffix.lower() in _IMAGE_SUFFIXES and capabilities.images
        if not image and not capabilities.files:
            raise ValueError('通道不支持此附件类型')
        limit = capabilities.max_image_bytes if image else capabilities.max_file_bytes
        prepared.append(verify_attachment_content(attachment, max_bytes=limit))
    return tuple(prepared)


# LLM: 回执只记录typed附件ID，不接受JSON blob或路径授权；不记录附件绝对路径；channel/target 必须使用已规范化可信上下文。
# 函数用途: 构造尚未发送的基础投递回执。
def _initial_receipt(
    context: DeliveryContext,
    envelope: ReplyEnvelope,
    *,
    channel: str,
    target: str,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        channel=channel,
        target=target,
        content=str(envelope.content or ""),
        thread_id=context.thread_id,
        task_id=context.task_id,
        attachment_ids=tuple(str(item.artifact_id or "") for item in envelope.attachments if isinstance(item, ChannelAttachment)),
        evidence_refs=tuple(
            str(item)
            for item in envelope.evidence_refs
            if str(item or "").strip()
        ),
        receipt_id=_delivery_receipt_id(
            context,
            envelope,
            channel=channel,
            target=target,
        ),
    )


def _delivery_receipt_id(
    context: DeliveryContext,
    envelope: ReplyEnvelope,
    *,
    channel: str,
    target: str,
) -> str:
    payload = "|".join(
        (
            str(context.idempotency_key or context.request_id or context.task_id or ""),
            channel,
            target,
            str(envelope.content or ""),
            ",".join(str(item or "") for item in envelope.evidence_refs),
            ",".join(str(item.artifact_id or "") for item in envelope.attachments if isinstance(item, ChannelAttachment)),
        )
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:20]


def _attachment_idempotency_key(
    context: DeliveryContext,
    attachment: object,
    index: int,
) -> str:
    base = str(context.idempotency_key or context.request_id or "").strip()
    if not base:
        return ""
    identity = ":".join(
        (
            base,
            str(max(0, int(index))),
            str(getattr(attachment, "artifact_id", "") or ""),
            str(getattr(attachment, "sha256", "") or ""),
        )
    )
    return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()


__all__ = ["DeliveryService"]
