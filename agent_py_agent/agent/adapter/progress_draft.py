# LLM: 单条进度内容、ID和游标只存在原PendingGatewayReply；新批次去旧页脚及裁剪残行，冻结外发重放不改，错误不能吞最终答复。
# 模块用途: 合并流式进度批次，并沿原 claim/dispatch/CAS 保存可恢复的编辑消息身份。
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field, replace


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
    steps: tuple[dict, ...] = field(default=(), kw_only=True)
    render_kind: str = field(default='text', kw_only=True)
    source_revision: int = field(default=0, kw_only=True)
    verbose_level: str = field(default='', kw_only=True)

    # LLM: 旧 tuple 解包只投影消息和游标；新的 stream mode 由 worker 显式读取。
    # 函数用途: 保持原 poll_progress 调用方可以解包批次。
    def __iter__(self):
        yield list(self.messages)
        yield self.next_cursor


# LLM: mode/ID沿原记录；新批次先脱敏再在页脚预算内按完整行裁剪，单行保留尾部语义；冻结dispatch与final全文不改。
# 函数用途: 去除增量旧时间戳并合并发送一条有界进度，失败沿原dispatch恢复，不另建预览。
def deliver_stream_progress(worker: object, record: object, batch: GatewayProgressBatch, *, force: bool = False,
                            max_step_identities: int = 4096):
    draft = dict(record.progress_draft)
    if batch.verbose_level in {'off', 'on', 'full', 'stream'} and draft.get('verbose_level') != batch.verbose_level:
        draft['verbose_level'] = batch.verbose_level
        record = worker.store.commit_claim(record, replace(record, progress_draft=draft),
            owner=worker._claim_owner, epoch=record.claim_epoch, release=False)
        if record is None:
            return None
        draft = dict(record.progress_draft)
    # 首发类型先冻结到原 pending；旧 message_id 没有类型字段时只能继续 text。
    render_kind = str(draft.get('render_kind') or ('text' if draft.get('message_id') or draft.get('verbose_level') == 'off' else batch.render_kind))
    if draft.get("disabled"):
        return worker._commit_progress(record, batch.next_cursor)
    if not record.dispatch and not draft.get('reconcile_pending') and render_kind == 'card' and batch.next_cursor <= record.progress_cursor and (
            batch.source_revision <= int(draft.get('source_revision') or 0)):
        return record
    if not force and not record.dispatch and time.time() < float(draft.get("retry_at") or 0):
        return record
    if record.dispatch:
        text = str(draft.get("content") or "")
        next_cursor = int(record.dispatch["next_cursor"])
    elif draft.get('reconcile_pending'):
        # 原 key 对应的卡片结构、revision 和正文全部冻结；身份核实后再合入新事件。
        text = str(draft.get('content') or '')
        next_cursor = int(draft.get('frozen_next_cursor', record.progress_cursor))
    else:
        elapsed = time.time() - float(draft.get("sent_at") or 0)
        if not force and 0 <= elapsed < 1.0:
            return record
        if not batch.messages and not (render_kind == 'card' and batch.steps):
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
        if render_kind == 'card':
            steps = {str(row['step_id']): dict(row) for row in draft.get('steps', ()) if isinstance(row, dict) and row.get('step_id')}
            seen = dict.fromkeys(draft.get('step_id_hashes', ()) or
                [hashlib.sha256(key.encode()).hexdigest() for key in steps])
            truncation = dict(draft.get('truncation') or {})
            observed = max(int(truncation.get('observed_steps') or 0), len(seen))
            complete = draft.get('step_count_complete', not draft.get('truncated', False))
            for row in batch.steps:
                if isinstance(row, dict) and row.get('step_id'):
                    identity = str(row['step_id'])
                    digest = hashlib.sha256(identity.encode()).hexdigest()
                    if digest not in seen:
                        if len(seen) < max(1, max_step_identities):
                            seen[digest] = None
                            observed += 1
                        else:
                            complete = False
                    steps[identity] = dict(row)
            truncation.update(observed_steps=observed, events_truncated=not complete)
            draft.update(steps=list(steps.values())[-50:], truncated=draft.get('truncated', False) or len(steps) > 50,
                step_id_hashes=list(seen), step_count_complete=complete, truncation=truncation, request_phase='updated',
                source_revision=max(int(draft.get('source_revision') or 0), batch.source_revision or batch.next_cursor))
            if not text:
                text = '执行记录更新'
                draft['content'] = text
        draft['render_kind'] = render_kind
        updated = replace(record, progress_draft=draft)
        record = worker.store.commit_claim(record, updated, owner=worker._claim_owner, epoch=record.claim_epoch, release=False)
        if record is None:
            return None
    sent_record, _sent = worker._dispatch_message(record, text, (f"progress:{record.progress_cursor}", next_cursor))
    return worker._commit_progress(sent_record, next_cursor) if sent_record is not None else None


# LLM: 原 pending 提供可信回复路由与已发送 ID；所有净化、目标校验和 provider 调用留在唯一 DeliveryService。
# 函数用途: 将原进度账的消息身份与内容交给统一渠道服务，不启动另一会话。
def send_gateway_draft(service: object, pending: object, text: str, *, final: bool = False):
    from ..delivery import ReplyEnvelope
    from ..delivery.service import ChannelDeliveryContext as DeliveryContext

    context = DeliveryContext(channel=pending.channel, target=pending.user_id, mode="reply",
        reply_to=pending.message_id, request_id=pending.request_id,
        idempotency_key=str(pending.dispatch.get("message_key") or ""),
        reply_policy=pending.reply_policy, provider_thread_id=pending.provider_thread_id,
        root_id=pending.root_id, parent_id=pending.parent_id)
    options = {}
    draft = pending.progress_draft
    if draft.get('render_kind') == 'card':
        options = {'render_kind': 'card', 'snapshot': {'request_id': pending.request_id,
            'source_revision': draft.get('source_revision', pending.progress_cursor),
            'steps': draft.get('steps', ()), 'truncated': draft.get('truncated', False),
            'truncation': draft.get('truncation', {}), 'final': final,
            'request_phase': 'completed' if final else draft.get('request_phase', 'updated'),
            'total_steps': (draft.get('truncation', {}).get('observed_steps') if final and draft.get('step_count_complete') else draft.get('total_steps'))}}
    if pending.reply_policy:
        options['reply_policy'] = pending.reply_policy
    return service.deliver_progress(context, ReplyEnvelope(content=text),
        message_id=str(draft.get('display_message_id') or draft.get("message_id") or ""), **options)


# LLM: 保留原 on/full 的独立进度身份，stream 才使用可编辑草稿；两条展示分支共享原外发状态机和 DeliveryService。
# 函数用途: 选择显式进度模式，普通消息保持原逐条行为，流式进度使用已持久的消息 ID。
def send_gateway_progress(manager: object, pending: object, text: str):
    from .manager import _gateway_delivery_key, _pending_gateway_message

    if pending.progress_draft.get("mode") == "stream":
        return send_gateway_draft(manager._delivery_service, pending, text)
    return manager._send_gateway_reply(_pending_gateway_message(pending), pending.request_id, text,
        idempotency_key=_gateway_delivery_key(message_id=pending.message_id, request_id=pending.request_id,
            phase="progress", progress_cursor=pending.progress_cursor))


def project_execution_steps(events, *, request_id: str) -> tuple[dict, ...]:
    """保留真实结构工具事件；模型说明、自然语言状态和正文不产生执行步骤。"""
    from ..conversation.channels import project_host_paths_for_channel, project_user_reply

    steps = []
    for event in events:
        if not isinstance(event, dict) or event.get('kind') in {'assistant_commentary', 'model_liveness', 'model_delta', 'model_draft'}:
            continue
        tool = event.get('tool') or event.get('tool_name')
        phase = event.get('phase')
        identity = event.get('call_id')
        if not identity and type(event.get('round')) is int and type(event.get('call_index')) is int:
            identity = f"{request_id}:{event['round']}:{event['call_index']}"
        if not isinstance(tool, str) or not isinstance(phase, str) or not identity:
            continue
        row = {'step_id': str(identity)[:256], 'tool_name': tool[:96], 'phase': phase[:64],
            'label': project_host_paths_for_channel(project_user_reply(tool).content, 'feishu')[:160]}
        for key in ('ok', 'handler_executed'):
            if type(event.get(key)) is bool:
                row[key] = event[key]
        if type(event.get('duration_ms')) is int and event['duration_ms'] >= 0:
            row['duration_ms'] = event['duration_ms']
        steps.append(row)
    return tuple(steps)
