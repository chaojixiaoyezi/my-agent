# LLM: 事件结构同时服务审计和恢复视图，字段变化要保留兼容读取。
# 模块用途: LocalStore 事件写入和时间线查询。

from __future__ import annotations

"""implements LocalStore audit event persistence and timeline row hydration.

给人看的解释：
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


# LLM: LocalStoreEventMixin 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStoreEventMixin 封装 LocalStore 本地事实索引 的一组相关操作，供上层组合调用。
class LocalStoreEventMixin:

    # LLM: LocalStoreEventMixin.timeline 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 timeline 步骤，并保持调用方依赖的数据形状。
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

    # LLM: LocalStoreEventMixin.record_event 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 record_event 写入本地存储并保留后续查询需要的字段。
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

    # LLM: LocalStoreEventMixin._record_event 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 record_event 写入本地存储并保留后续查询需要的字段。
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

    # LLM: LocalStoreEventMixin._timeline_row 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 timeline_row 步骤，并保持调用方依赖的数据形状。
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
