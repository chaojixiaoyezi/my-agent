# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parsers.common import utc_now


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 BatchFinish 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 BatchFinish 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class BatchFinish:
    # LLM: Batch finalization is bundled so ingest accounting can grow safely.
    batch_id: str
    status: str
    event_count: int
    duplicate_count: int
    dead_letter_count: int
    manifest_path: str


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 DedupStore 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 DedupStore 的持久化入口，把路径、读写和查询操作集中到同一对象。
class DedupStore:
    """SQLite-backed batch and event dedup ledger."""

    _init_locks_guard = threading.Lock()
    _init_locks: dict[str, threading.Lock] = {}

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema_once()

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _init_schema_once 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 init schema once 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _init_schema_once(self) -> None:
        """Serialize first-time schema bootstrap for the same SQLite path.

        并发 ingest 会在多个线程里同时 new `DedupStore(root/"dedup.sqlite3")`。
        SQLite 在多个连接同时切 WAL / 建表时偶发 `database is locked`，这里
        先按数据库路径串行化初始化，避免把启动竞态暴露给上层 pipeline。"""
        key = str(self.db_path.resolve())
        with self._init_locks_guard:
            lock = self._init_locks.setdefault(key, threading.Lock())
        with lock:
            self._init_schema()

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 begin_batch 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 begin batch 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def begin_batch(
        self,
        *,
        batch_id: str,
        source_id: str,
        content_hash: str,
        source_path: str,
    ) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO log_ingest_batches (
                    batch_id, source_id, content_hash, source_path, status,
                    created_at, updated_at, event_count, duplicate_count,
                    dead_letter_count, manifest_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, NULL)
                """,
                (batch_id, source_id, content_hash, source_path, "started", now, now),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    UPDATE log_ingest_batches
                    SET updated_at = ?, status = CASE
                        WHEN status = 'stored' THEN status
                        ELSE 'started'
                    END
                    WHERE batch_id = ?
                    """,
                    (now, batch_id),
                )
            return cur.rowcount == 1

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 finish_batch 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 finish batch 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def finish_batch(
        self,
        *,
        params: BatchFinish | None = None,
        batch_id: str = "",
        status: str = "",
        event_count: int = 0,
        duplicate_count: int = 0,
        dead_letter_count: int = 0,
        manifest_path: str = "",
    ) -> None:
        if params is None:
            params = BatchFinish(
                batch_id=str(batch_id),
                status=str(status),
                event_count=int(event_count),
                duplicate_count=int(duplicate_count),
                dead_letter_count=int(dead_letter_count),
                manifest_path=str(manifest_path),
            )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE log_ingest_batches
                SET status = ?, updated_at = ?, event_count = ?,
                    duplicate_count = ?, dead_letter_count = ?,
                    manifest_path = ?
                WHERE batch_id = ?
                """,
                (
                    params.status,
                    utc_now(),
                    params.event_count,
                    params.duplicate_count,
                    params.dead_letter_count,
                    params.manifest_path,
                    params.batch_id,
                ),
            )

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 is_duplicate 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 is duplicate 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def is_duplicate(self, dedup_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM log_event_dedup WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
        return row is not None

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 mark_event 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 mark event 相关记录，集中处理目标路径、格式化和状态更新。
    def mark_event(
        self,
        *,
        dedup_key: str,
        event_id: str,
        source_id: str,
        batch_id: str,
    ) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO log_event_dedup (
                    dedup_key, event_id, source_id, first_batch_id,
                    first_seen_at, last_seen_at, seen_count
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (dedup_key, event_id, source_id, batch_id, now, now),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    UPDATE log_event_dedup
                    SET last_seen_at = ?, seen_count = seen_count + 1
                    WHERE dedup_key = ?
                    """,
                    (now, dedup_key),
                )
            return cur.rowcount == 1

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 note_duplicate 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 note duplicate 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def note_duplicate(self, dedup_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE log_event_dedup
                SET last_seen_at = ?, seen_count = seen_count + 1
                WHERE dedup_key = ?
                """,
                (utc_now(), dedup_key),
            )

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 stats 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 stats 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            event_count = conn.execute("SELECT COUNT(*) FROM log_event_dedup").fetchone()[0]
            batch_count = conn.execute("SELECT COUNT(*) FROM log_ingest_batches").fetchone()[0]
        return {"event_dedup_count": event_count, "batch_count": batch_count}

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _init_schema 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 init schema 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS log_ingest_batches (
                    batch_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    dead_letter_count INTEGER NOT NULL DEFAULT 0,
                    manifest_path TEXT
                );

                CREATE TABLE IF NOT EXISTS log_event_dedup (
                    dedup_key TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    first_batch_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_log_event_dedup_source
                ON log_event_dedup(source_id, last_seen_at);
                """
            )

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _connect 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 connect 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()
