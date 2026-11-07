# LLM: 原事件构造/SQL/JSONL仍唯一；C只替换已持锁固定append叶，不改事件身份或事务次序。
# 模块用途: 持久化原本地事件，支持C在SQL事务内无重复取锁追加和显式有界维护周期。

from __future__ import annotations

"""implements LocalStore audit event persistence and timeline row hydration.

这个文件只管'流水账'。
每次重要写入都会进 SQLite events 表，也会追加到 JSONL 文件，方便程序查也方便人排查。
"""

import json
import os
import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from ..common.json_io import (
    _tighten_private_file,
    append_private_jsonl_records_unlocked,
    locked_json_path,
)
from ..common.nofollow_fs import ensure_private_dir
from ..memory_archive._storage_dates import _coerce_datetime, _date_key
from ..user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES, STORAGE_LANE_SLICE_ENTRIES
from .models import LocalStoreEvent, LocalTimelineItem

# 只保留最近一个文件身份；多 Store 交错时整份替换，避免热路径缓存随历史增长。
_event_first_day_cache: dict[tuple[int, int], str] = {}


def event_segments_dir(events_path: Path) -> Path:
    """封存段沿活动文件的配置路径派生，避免另建一份路径权威。"""
    return events_path.parent / f"{events_path.stem}-daily"


def iter_event_jsonl_paths(events_path: Path) -> list[Path]:
    """供显式读取遍历旧段到活动文件；轮转热路径不调用此目录枚举。"""
    try:
        with os.scandir(event_segments_dir(events_path)) as entries:
            paths = sorted(Path(entry.path) for entry in entries
                           if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False))
    except FileNotFoundError:
        paths = []
    if events_path.exists():
        paths.append(events_path)
    return paths


def _event_line_day(line: bytes) -> str | None:
    """坏行或无效时间不能借归档日期的今日兜底误封存，日期口径仍复用原实现。"""
    try:
        record = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(record, dict) or _coerce_datetime(record.get("created_at")) is None:
        return None
    return _date_key(record["created_at"])


def rotate_event_jsonl_if_due_unlocked(events_path: Path) -> Path | None:
    """调用方必须持原 events_path 的 JSONL 锁；只 rename，禁止重写或截断流水。"""
    global _event_first_day_cache
    try:
        info = events_path.stat()
    except FileNotFoundError:
        return None
    if info.st_size < STORAGE_LANE_SLICE_BYTES:
        return None
    identity = (info.st_dev, info.st_ino)
    first_day = _event_first_day_cache.get(identity)
    today = date.today().isoformat()
    if first_day == today:
        return None
    with events_path.open("rb") as handle:
        if first_day is None:
            first_line = handle.readline(64 * 1024)
            first_day = _event_line_day(first_line) if first_line.endswith(b"\n") else None
            if first_day is None:
                return None
            _event_first_day_cache = {identity: first_day}
        if first_day == today:
            return None
        # 只在尾部窗口内找最后一个完整行；窗口截断的行不能充当封存日期。
        offset = max(0, info.st_size - 64 * 1024)
        handle.seek(offset)
        tail = handle.read(64 * 1024)
        end = tail.rfind(b"\n")
        start = tail.rfind(b"\n", 0, end) + 1
        if end < 0 or (start == 0 and offset > 0):
            return None
        last_day = _event_line_day(tail[start:end])
    if last_day is None:
        return None
    segment_dir = event_segments_dir(events_path)
    ensure_private_dir(segment_dir)
    # 不扫封存历史，只探测固定数量的同日名字，锁保持在活动文件旁车上。
    for number in range(STORAGE_LANE_SLICE_ENTRIES + 1):
        suffix = f".{number}" if number else ""
        target = segment_dir / f"{last_day}{suffix}.jsonl"
        if not os.path.lexists(target):
            # 沿用原追加的权限收紧，避免宽权限旧流被 rename 后跳过这一步。
            _tighten_private_file(events_path)
            os.rename(events_path, target)
            _event_first_day_cache = {}
            return target
    return None


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
        records = [{
            "event_id": event.event_id,
            "event_type": event.event_type,
            "record_id": event.record_id,
            "payload": event.payload,
            "created_at": event.created_at,
        }]
        if append_records is None:
            # 保持 SQL 写事务在先、JSONL 锁在后；封存和追加共用原锁，不能嵌套自取锁。
            with locked_json_path(self.events_path):
                rotate_event_jsonl_if_due_unlocked(self.events_path)
                append_private_jsonl_records_unlocked(self.events_path, records, sort_keys=True)
        else:
            append_records(self.events_path, records, sort_keys=True)
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
