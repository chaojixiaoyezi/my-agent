"""外发后的 canonical 关联和原会话通知；不执行运输，也不重跑业务。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .execution_threads import execution_origin_thread
from .outbound_message_index import OutboundMessageIndex


def outbound_message_ids(receipt, *, frozen_metadata=None) -> tuple[str, ...]:
    """优先真实提供方组件编号，恢复只取外发前冻结的相同集合。"""
    frozen = frozen_metadata if isinstance(frozen_metadata, dict) else {}
    if isinstance(frozen.get("channel_message_ids"), list):
        return tuple(dict.fromkeys(value for value in frozen["channel_message_ids"]
                                   if isinstance(value, str) and value))
    components = getattr(receipt, "component_receipts", ())
    if isinstance(components, (list, tuple)) and components:
        return tuple(dict.fromkeys(row["provider_message_id"] for row in components
            if isinstance(row, dict) and row.get("phase") == "send_confirmed"
            and row.get("effect_outcome") == "confirmed"
            and isinstance(row.get("provider_message_id"), str) and row["provider_message_id"]))
    value = getattr(receipt, "channel_message_id", "") or getattr(receipt, "receipt_id", "")
    return (value,) if isinstance(value, str) and value else ()


def _owned(thread, home) -> bool:
    return (thread.owner_id == str(home.owner_id) and bool(thread.owner_home)
            and Path(thread.owner_home) == Path(home.owner_home_dir))


def commit_outbound_notice(agent, store, request, context, message):
    """先建有界查找投影，再幂等同步原会话；任一步失败交回原冻结重试链。"""
    home = getattr(agent, "home_paths", None)
    if home is None or not getattr(home, "owner_home_dir", None) or not getattr(home, "owner_id", None):
        return
    ids = message.metadata.get("channel_message_ids")
    if not isinstance(ids, list) or not ids:
        return
    # 旧缺 owner 字段的会话仍沿旧交付，不以投影补造其权限。
    canonical = store.threads.require(message.thread_id)
    if not _owned(canonical, home):
        return
    chat_id = context.conversation_id or context.target
    if not chat_id:
        return
    if context.channel == "feishu":
        from ..adapter.protocol import parse_feishu_conversation_address

        chat_id = parse_feishu_conversation_address(chat_id)[0]
    refs = message.metadata.get("evidence_refs", [])
    workflow_run_id = message.metadata.get("workflow_run_id")
    entry = {"schema_version": "outbound_message.v1", "channel": context.channel,
        "chat_id": chat_id, "thread_id": message.thread_id, "conversation_message_id": message.message_id,
        "task_id": message.metadata.get("task_id") or None,
        "workflow_run_id": workflow_run_id if isinstance(workflow_run_id, str) else None,
        "evidence_refs": list(refs) if isinstance(refs, list) else [],
        "receipt_id": message.metadata.get("receipt_id") or None,
        "sent_at": datetime.fromtimestamp(message.created_at, timezone.utc).isoformat()}
    index = OutboundMessageIndex(home.owner_home_dir)
    for identity in ids:
        index.record({**entry, "channel_message_id": identity})
    work = store.threads.require(request.thread_id)
    if not _owned(work, home):
        return
    origin = execution_origin_thread(store.threads.require, work)
    if not _owned(origin, home) or origin.thread_id == message.thread_id:
        return
    notice = {**entry, "channel_message_ids": list(ids)}
    key = hashlib.sha256(json.dumps([entry["channel"], chat_id, entry["receipt_id"] or ids],
                                    ensure_ascii=False).encode()).hexdigest()
    links = {"task_id": entry["task_id"], "workflow_run_id": entry["workflow_run_id"],
             "evidence_refs": [ref[:240] for ref in entry["evidence_refs"][:8]]}
    store.messages.append_once({"thread_id": origin.thread_id, "role": "assistant", "channel": context.channel,
        "content": "[主动通知摘要]\n" + message.content[:600] + "\n" + json.dumps(links, ensure_ascii=False),
        "metadata": {"origin_notice": notice}}, dedupe_key=f"origin_notice:{key}")
