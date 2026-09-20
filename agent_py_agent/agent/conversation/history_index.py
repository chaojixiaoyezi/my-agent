from __future__ import annotations

"""Derived search index for owner-scoped authoritative conversation messages."""

from typing import TYPE_CHECKING

from .channels import project_user_reply
from .models import MessageLogEntry

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from .store import ConversationStore


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
    rows, errors = store.messages.recent_report(thread_id, limit=0)
    if errors:
        raise OSError("conversation transcript could not be indexed reliably")
    for row in rows:
        index_conversation_message(agent, store, row)
    indexed.add(thread_id)


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
    existing = agent.local_store.get_record(record_id)
    if existing is not None and existing.content == content and existing.metadata == metadata:
        return
    agent.local_store.upsert_record(
        source_type="conversation_message",
        source_id=source_id,
        title=f"Conversation {row.role}",
        content=content,
        metadata=metadata,
        visibility="private",
        record_id=record_id,
    )


__all__ = ["ensure_thread_history_indexed", "index_conversation_message"]
