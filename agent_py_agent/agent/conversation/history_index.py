# LLM: canonical线程的派生搜索索引，正文/元数据精确比较，首次批次共享短连接；错误不得标记已完成。
# 模块用途: 不物化全历史地同步搜索投影，既有索引不再逐消息水合整条记录。
from __future__ import annotations

"""Derived search index for owner-scoped authoritative conversation messages."""

import json
from typing import TYPE_CHECKING

from ..local_storage.records import LocalRecordInput
from ..local_storage.schema import _read_local_state, _write_local_state
from .channels import project_user_reply
from .models import MessageLogEntry

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from .store import ConversationStore


# 水位保存在派生索引所在的 local.db；代理回收后继续增量读取，失败不推进，也不标记本轮完成。
def ensure_thread_history_indexed(
    agent: SimpleAgent,
    store: ConversationStore,
    thread_id: str,
) -> None:
    indexed = getattr(agent, "_conversation_indexed_threads", None)
    if indexed is None:
        indexed = set()
        agent._conversation_indexed_threads = indexed
    if thread_id in indexed:
        return
    local = agent.local_store
    key = "conversation_history_indexed:" + thread_id
    with local.connection_batch():
        with local._connection() as conn:
            value = _read_local_state(conn, key)
        try:
            watermark = json.loads(value) if value is not None else None
        except (ValueError, TypeError):
            watermark = None
        errors, watermark = store.messages.visit_from_report(
            thread_id, watermark, lambda row: index_conversation_message(agent, store, row),
        )
        if errors:
            raise OSError("conversation transcript could not be indexed reliably")
        value = json.dumps(watermark, sort_keys=True)
        if local.coordinator_io:
            # 整笔 DML/commit 消费当次 _local_write 写门，不把读取连接带到门外写入。
            local._coordinator_transaction(lambda conn: _write_local_state(conn, key, value))
        else:
            with local._connection() as conn:
                _write_local_state(conn, key, value)
                conn.commit()
    indexed.add(thread_id)


# LLM: 只索引原user/assistant公开正文及明确验证事实；比较实际正文、metadata，不把native信封放进搜索正文。
# 函数用途: 精确复用未变化记录；新增或变化仍调用原upsert及审计、FTS写入。
def index_conversation_message(
    agent: SimpleAgent,
    store: ConversationStore,
    row: MessageLogEntry,
) -> None:
    if row.role not in {"user", "assistant"} or not row.content.strip():
        return
    content = project_user_reply(row.content).content if row.role == "assistant" else row.content
    source_id = f"{row.thread_id}:{row.message_id}"
    metadata = {
        "thread_id": row.thread_id,
        "message_id": row.message_id,
        "role": row.role,
        "channel": row.channel,
        "channel_message_id": row.channel_message_id,
        "created_at": row.created_at,
        "authoritative_transcript": str(store.storage.message_path(row.thread_id)),
    }
    operation_verification = row.metadata.get("operation_verification")
    if (
        isinstance(operation_verification, dict)
        and operation_verification.get("schema") == "operation_verification.public.v1"
    ):
        metadata["operation_verification"] = operation_verification
    record_id = agent.local_store.make_record_id("conversation_message", source_id)
    record = LocalRecordInput(
        source_type='conversation_message', source_id=source_id, title=f'Conversation {row.role}',
        content=content, metadata=metadata, visibility='private', record_id=record_id,
    )
    if agent.local_store.record_matches(record):
        return
    agent.local_store.upsert_record(record)


__all__ = ["ensure_thread_history_indexed", "index_conversation_message"]
