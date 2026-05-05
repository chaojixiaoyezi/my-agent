from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..parsers.common import utc_now


class DedupStore:
    """SQLite-backed batch and event dedup ledger."""

    _init_locks_guard = threading.Lock()
    _init_locks: dict[str, threading.Lock] = {}

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema_once()

    def _init_schema_once(self) -> None:
        """Serialize first-time schema bootstrap for the same SQLite path.

        并发 ingest 会在多个线程里同时 new `DedupStore(root/"dedup.sqlite3")`。
        SQLite 在多个连接同时切 WAL / 建表时偶发 `database is locked`，这里
        先按数据库路径串行化初始化，避免把启动竞态暴露给上层 pipeline。
        """
        key = str(self.db_path.resolve())
        with self._init_locks_guard:
            lock = self._init_locks.setdefault(key, threading.Lock())
        with lock:
            self._init_schema()

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

    def finish_batch(
        self,
        *,
        batch_id: str,
        status: str,
        event_count: int,
        duplicate_count: int,
        dead_letter_count: int,
        manifest_path: str,
    ) -> None:
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
                    status,
                    utc_now(),
                    event_count,
                    duplicate_count,
                    dead_letter_count,
                    manifest_path,
                    batch_id,
                ),
            )

    def is_duplicate(self, dedup_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM log_event_dedup WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
        return row is not None

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

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            event_count = conn.execute("SELECT COUNT(*) FROM log_event_dedup").fetchone()[0]
            batch_count = conn.execute("SELECT COUNT(*) FROM log_ingest_batches").fetchone()[0]
        return {"event_dedup_count": event_count, "batch_count": batch_count}

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
