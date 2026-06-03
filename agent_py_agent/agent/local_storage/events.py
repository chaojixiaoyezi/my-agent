
from __future__ import annotations

"""implements LocalStore audit event persistence and timeline row hydration.

这个文件只管'流水账'。
每次重要写入都会进 SQLite events 表，也会追加到 JSONL 文件，方便程序查也方便人排查。
"""

import json
import sqlite3
import time
import uuid
from typing import Any

from ..file_io import append_jsonl
from .models import LocalStoreEvent, LocalTimelineItem


class LocalStoreEventMixin:

    def timeline(
        self,
        *,
        limit: int = 20,
        source_type: str | None = None,
        event_type: str | None = None,
    ) -> list[LocalTimelineItem]:

        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("records.source_type = ?")
            params.append(source_type)
        if event_type:
            clauses.append("events.event_type = ?")
            params.append(event_type)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    events.event_id,
                    events.event_type,
                    events.record_id,
                    events.payload_json,
                    events.created_at,
                    records.source_type,
                    records.source_id,
                    records.title
                FROM events
                LEFT JOIN records ON records.id = events.record_id
                {where}
                ORDER BY events.created_at DESC, events.seq DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._timeline_row(row) for row in rows]

    def record_event(
        self,
        event_type: str,
        *,
        record_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> LocalStoreEvent:
        """追加一条自定义审计事件。"""

        with self._connection() as conn:
            event = self._record_event(conn, event_type, record_id, payload or {})
            conn.commit()
        return event

    def _record_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        record_id: str,
        payload: dict[str, Any],
    ) -> LocalStoreEvent:
        event = LocalStoreEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            record_id=record_id,
            payload=payload,
            created_at=time.time(),
        )
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO events(event_id, event_type, record_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event.event_id, event.event_type, event.record_id, payload_json, event.created_at),
        )
        append_jsonl(
            self.events_path,
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "record_id": event.record_id,
                "payload": event.payload,
                "created_at": event.created_at,
            },
            sort_keys=True,
        )
        return event

    def _timeline_row(self, row: sqlite3.Row) -> LocalTimelineItem:
        payload = json.loads(row["payload_json"] or "{}")
        source_type = row["source_type"] or str(payload.get("source_type", ""))
        source_id = row["source_id"] or str(payload.get("source_id", ""))
        title = row["title"] or str(payload.get("title", ""))
        return LocalTimelineItem(
            event_id=row["event_id"],
            event_type=row["event_type"],
            record_id=row["record_id"],
            source_type=source_type,
            source_id=source_id,
            title=title,
            payload=payload,
            created_at=float(row["created_at"]),
        )
