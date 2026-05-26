# LLM: Message log operations stay separate from thread routing.
# 模块用途: 追加和读取长期会话消息流水。

from __future__ import annotations

from dataclasses import replace

from ..io.jsonl import append_jsonl
from .models import MessageLogEntry, new_id
from .store_common import now as current_time
from .store_common import read_jsonl
from .store_threads import ConversationThreadStore


class ConversationMessageStore(ConversationThreadStore):
    def append_message(self, request: dict) -> MessageLogEntry:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        entry = MessageLogEntry(
            message_id=new_id("msg"),
            thread_id=thread.thread_id,
            role=str(request.get("role") or ""),
            content=str(request.get("content") or ""),
            channel=str(request.get("channel") or "internal"),
            channel_message_id=str(request.get("channel_message_id") or ""),
            created_at=current_time(request.get("now")),
            metadata=request.get("metadata") or {},
        )
        append_jsonl(self._message_path(thread_id), entry.to_dict(), sort_keys=True)
        self._write_thread(replace(thread, updated_at=entry.created_at))
        return entry

    def recent_messages(self, thread_id: str, *, limit: int = 20) -> list[MessageLogEntry]:
        entries = [MessageLogEntry.from_dict(row) for row in read_jsonl(self._message_path(thread_id))]
        return entries if limit <= 0 else entries[-limit:]
