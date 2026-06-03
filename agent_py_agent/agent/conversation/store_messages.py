
from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..io.jsonl import append_jsonl
from ..runtime_errors import runtime_error_report
from .models import MessageLogEntry, new_id
from .store_common import now as current_time
from .store_common import read_jsonl_report
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
        entries, _errors = self.recent_messages_report(thread_id, limit=limit)
        return entries

    def recent_messages_report(
        self,
        thread_id: str,
        *,
        limit: int = 20,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        report = read_jsonl_report(
            self._message_path(thread_id),
            context="conversation.messages.read",
        )
        entries, parse_errors = _message_entries(report.rows)
        selected = entries if limit <= 0 else entries[-limit:]
        return selected, [*report.load_errors, *parse_errors]


def _message_entries(rows: list[dict[str, Any]]) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
    entries: list[MessageLogEntry] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            entries.append(MessageLogEntry.from_dict(row))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.messages.parse")
            report["row_index"] = index
            errors.append(report)
    return entries, errors
