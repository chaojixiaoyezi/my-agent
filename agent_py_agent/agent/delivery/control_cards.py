# LLM: 原生控制卡只承运宿主结构化交互；审批和凭据的授权仍在各自原账；禁止普通文本回落或无回执推定送达。
# 模块用途: 沿唯一 DeliveryService 的目标与能力前检发送私聊卡片，返回供应商真实消息和聊天身份。
from __future__ import annotations

from dataclasses import dataclass

from ..conversation.channels import DeliveryContext, DeliveryReceipt, ReplyEnvelope


# LLM: message_id/chat_id 只来自 provider 成功回执，调用方保存到原交互记录；此回执不是用户批准。
# 类用途: 记录一张原生控制卡是否实际发出及其后续回调必须核对的身份。
@dataclass(frozen=True)
class ControlCardDeliveryReceipt:
    ok: bool
    message_id: str = ""
    chat_id: str = ""
    error_code: str = ""
    provider_code: int | None = None
    effect_outcome: str = "not_started"


# LLM: 只调用已声明 control_cards 的 adapter；明确平台拒绝保留安全数字code与未执行事实，异常或缺成功身份才是未知运输。
# 函数用途: 发送一张私聊审批/输入卡，使用原目标验证、去重键和 provider 消息回执。
def deliver_control_card(service, context: DeliveryContext, card: dict, *, summary: str) -> ControlCardDeliveryReceipt:
    envelope = ReplyEnvelope(content=summary)
    prepared = service._prepare(context, envelope, progress=True)
    if isinstance(prepared, DeliveryReceipt):
        return ControlCardDeliveryReceipt(False, error_code=prepared.error_code or "CHANNEL_CONTROL_REJECTED")
    attempt = service._resolve_attempt(context, envelope, prepared)
    if isinstance(attempt, DeliveryReceipt):
        return ControlCardDeliveryReceipt(False, error_code=attempt.error_code or "CHANNEL_CONTROL_REJECTED")
    if not attempt.capabilities.control_cards:
        return ControlCardDeliveryReceipt(False, error_code="CHANNEL_CONTROL_UNSUPPORTED")
    try:
        receipt = attempt.adapter.send_control_card(context.target, card, idempotency_key=context.idempotency_key)
    except Exception:  # noqa: BLE001 - 传输故障不能被当作批准或转发到别的平台
        return ControlCardDeliveryReceipt(False, error_code="CHANNEL_CONTROL_IO_FAILED", effect_outcome="unknown")
    if isinstance(receipt, dict) and receipt.get("error_code") in {"CHANNEL_CONTROL_PROVIDER_REJECTED", "CHANNEL_CONTROL_AUTH_UNAVAILABLE"}:
        code = receipt.get("provider_code")
        return ControlCardDeliveryReceipt(False, error_code=receipt["error_code"],
            provider_code=code if isinstance(code, int) and not isinstance(code, bool) else None)
    if not isinstance(receipt, dict) or not receipt.get("message_id") or not receipt.get("chat_id"):
        return ControlCardDeliveryReceipt(False, error_code="CHANNEL_CONTROL_RECEIPT_MISSING", effect_outcome="unknown")
    return ControlCardDeliveryReceipt(True, str(receipt["message_id"]), str(receipt["chat_id"]), effect_outcome="confirmed")
