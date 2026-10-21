# LLM: 单条进度内容、ID和游标只存在原PendingGatewayReply；新批次去旧页脚及裁剪残行，冻结外发重放不改，错误不能吞最终答复。
# 模块用途: 合并流式进度批次，并沿原 claim/dispatch/CAS 保存可恢复的编辑消息身份。
from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace


# LLM: mode 是 Gateway 明确投影的结构字段，不能从进度正文解析；旧回调 tuple 沿原 append 行为。
# 类用途: 携带本批进度的文字、游标和显式展示模式。
@dataclass(frozen=True)
class GatewayProgressBatch:
    messages: tuple[str, ...]
    next_cursor: int
    mode: str = "append"
    replace_content: bool = False
    preview_max_chars: int | None = None
    updated_at: str = ""

    # LLM: 旧 tuple 解包只投影消息和游标；新的 stream mode 由 worker 显式读取。
    # 函数用途: 保持原 poll_progress 调用方可以解包批次。
    def __iter__(self):
        yield list(self.messages)
        yield self.next_cursor


# LLM: mode/ID沿原记录；新批次先脱敏再在页脚预算内按完整行裁剪，单行保留尾部语义；冻结dispatch与final全文不改。
# 函数用途: 去除增量旧时间戳并合并发送一条有界进度，失败沿原dispatch恢复，不另建预览。
def deliver_stream_progress(worker: object, record: object, batch: GatewayProgressBatch):
    draft = dict(record.progress_draft)
    if draft.get("disabled"):
        return worker._commit_progress(record, batch.next_cursor)
    if not record.dispatch and time.time() < float(draft.get("retry_at") or 0):
        return record
    if record.dispatch:
        text = str(draft.get("content") or "")
        next_cursor = int(record.dispatch["next_cursor"])
    else:
        elapsed = time.time() - float(draft.get("sent_at") or 0)
        if 0 <= elapsed < 1.0:
            return record
        if not batch.messages:
            return worker._commit_progress(record, batch.next_cursor) if batch.next_cursor != record.progress_cursor else record
        prefix = "" if batch.replace_content else str(draft.get("content") or "")
        if not batch.replace_content:
            prefix = re.sub(r"(?:\n|^)更新于 \d{2}:\d{2}:\d{2}\s*$", "", prefix)
        limit = batch.preview_max_chars if batch.preview_max_chars is not None else worker.stream_preview_max_chars
        from datetime import datetime

        footer = ""
        if batch.updated_at:
            footer = "\n更新于 " + datetime.fromisoformat(batch.updated_at).strftime("%H:%M:%S")
        limit = max(1, min(8000, int(limit)))
        from ..common.log_redaction import redact_sensitive_text

        content = redact_sensitive_text((prefix + "\n" + "\n".join(batch.messages)).strip(),
            redacted_marker="[REDACTED]", redact_assignment_labels=True)
        budget = max(0, limit - len(footer))
        tail = content[-budget:] if budget else ""
        if len(content) > budget and "\n" in tail:
            complete_lines = tail[tail.find("\n") + 1:]
            if complete_lines.strip():
                tail = complete_lines
        text = tail + footer[-limit:]
        next_cursor = batch.next_cursor
        draft.update(mode="stream", content=text)
        updated = replace(record, progress_draft=draft)
        record = worker.store.commit_claim(record, updated, owner=worker._claim_owner, epoch=record.claim_epoch, release=False)
        if record is None:
            return None
    sent_record, _sent = worker._dispatch_message(record, text, (f"progress:{record.progress_cursor}", next_cursor))
    return worker._commit_progress(sent_record, next_cursor) if sent_record is not None else None


# LLM: 原 pending 提供可信回复路由与已发送 ID；所有净化、目标校验和 provider 调用留在唯一 DeliveryService。
# 函数用途: 将原进度账的消息身份与内容交给统一渠道服务，不启动另一会话。
def send_gateway_draft(service: object, pending: object, text: str):
    from ..delivery import DeliveryContext, ReplyEnvelope

    context = DeliveryContext(channel=pending.channel, target=pending.user_id, mode="reply",
        reply_to=pending.message_id, request_id=pending.request_id,
        idempotency_key=str(pending.dispatch.get("message_key") or ""))
    return service.deliver_progress(context, ReplyEnvelope(content=text),
        message_id=str(pending.progress_draft.get("message_id") or ""))


# LLM: 保留原 on/full 的独立进度身份，stream 才使用可编辑草稿；两条展示分支共享原外发状态机和 DeliveryService。
# 函数用途: 选择显式进度模式，普通消息保持原逐条行为，流式进度使用已持久的消息 ID。
def send_gateway_progress(manager: object, pending: object, text: str):
    from .manager import _gateway_delivery_key, _pending_gateway_message

    if pending.progress_draft.get("mode") == "stream":
        return send_gateway_draft(manager._delivery_service, pending, text)
    return manager._send_gateway_reply(_pending_gateway_message(pending), pending.request_id, text,
        idempotency_key=_gateway_delivery_key(message_id=pending.message_id, request_id=pending.request_id,
            phase="progress", progress_cursor=pending.progress_cursor))
