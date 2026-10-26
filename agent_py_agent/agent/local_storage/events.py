# LLM: 原事件构造/SQL/JSONL仍唯一；C只替换已持锁固定append叶，不改事件身份或事务次序。
# 模块用途: 持久化原本地事件，支持C在SQL事务内无重复取锁追加和显式有界维护周期。

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

from ..common.json_io import append_private_jsonl_records
from .models import LocalStoreEvent, LocalTimelineItem


# LLM: 默认追加自取锁不变；C内部append固定原events_path/已持原锁，维护仅由显式有界周期调用。
# 类用途: 统一登记事件和推进有限维护批次，不在单条写入时扫描或复制第二审计状态。
class LocalStoreEventMixin:

    # LLM: 由后台/显式维护入口调用，绝不从_record_event热路径调用；每批沿原文件锁串行，C须有当次scope。
    # 函数用途: 在有限批次内推进两万条稳态裁剪，返回真实累计量，不启动线程或自动VACUUM。
    def maintain_events(self, *, max_batches: int = 1, before: float | None = None,
                        dry_run: bool = False) -> dict[str, int | bool]:
        cutoff = time.time() if before is None else before
        result = {"ok": True, "dry_run": bool(dry_run), "selected": 0, "deleted_db": 0, "deleted_jsonl": 0}
        for _ in range(max(0, int(max_batches))):
            batch = self.prune_events_steady_state(before=cutoff, dry_run=dry_run)
            for key in ("selected", "deleted_db", "deleted_jsonl"):
                result[key] += batch[key]
            if dry_run or batch["deleted_db"] == 0:
                break
        return result

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

    # LLM: 事件先落 SQLite、再私有追加 JSONL（0600/0700）；两条通道用同一 event 构造，JSONL 失败不静默。
    # LLM: append_records仅内部同events_path原已持锁叶；默认仍原自取锁追加，SQL INSERT→JSONL→调用方commit顺序不变。
    # 函数用途: 用同一原事件构造登记SQL并追加JSONL，C只替换重复取锁的末叶。
    def _record_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        record_id: str,
        payload: dict[str, Any],
        *,
        append_records=None,
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
        # 私有追加：0600/0700，存量宽权限事件流水下次写入即收紧；内容逐字节不变。
        (append_private_jsonl_records if append_records is None else append_records)(
            self.events_path,
            [{
                "event_id": event.event_id,
                "event_type": event.event_type,
                "record_id": event.record_id,
                "payload": event.payload,
                "created_at": event.created_at,
            }],
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
