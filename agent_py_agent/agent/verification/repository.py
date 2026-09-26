from __future__ import annotations

"""Owner-local SQLite ledger for passive verification evidence.

LLM: the repository records facts only.  It never schedules a command, blocks a
final response, or upgrades targeted evidence to full.
模块用途: 保存每个用户自己实际跑过的测试，并在后续改文件时把旧结果标为过期。
"""

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_MAX_EVIDENCE_AGE_DAYS = 30
_MAX_EVENTS_PER_TASK_ROOT = 100
_MAX_TOTAL_UNREFERENCED_EVENTS = 10_000
_DB_LOCK = threading.RLock()


@dataclass(frozen=True)
class VerificationContext:
    """Structured owner/thread/task identity for one evidence stream."""

    owner_ref: str
    thread_id: str
    task_id: str


@dataclass(frozen=True)
class VerificationEvidence:
    """One classified command result."""

    command: str
    canonical_command: str
    kind: str
    scope: str
    status: str
    exit_code: int
    cwd: str
    root: str
    output_summary: str = ""


class VerificationEvidenceRepository:
    """Persist verification facts inside exactly one owner home."""

    def __init__(self, owner_root: str | Path):
        self.owner_root = Path(owner_root).expanduser().resolve(strict=False)
        self.db_path = self.owner_root / "data" / "verification" / "evidence.sqlite3"

    # LLM: command results and state pointer commit in one transaction, so a
    # crash cannot expose a new "passed" pointer without its event row.
    # 函数用途: 记录一次测试命令结果，并把它设为当前最新证据。
    def record(self, context: VerificationContext, evidence: VerificationEvidence) -> dict[str, Any]:
        created_at = _utc_now()
        with _DB_LOCK, self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO verification_events(
                    created_at, owner_ref, thread_id, task_id, cwd, root, command,
                    canonical_command, kind, scope, status, exit_code, output_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    context.owner_ref,
                    context.thread_id,
                    context.task_id,
                    evidence.cwd,
                    evidence.root,
                    evidence.command,
                    evidence.canonical_command,
                    evidence.kind,
                    evidence.scope,
                    evidence.status,
                    evidence.exit_code,
                    evidence.output_summary,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("verification event insert did not return an id")
            event_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO verification_state(
                    owner_ref, thread_id, task_id, root, last_event_id, last_edit_at, changed_paths_json
                ) VALUES (?, ?, ?, ?, ?, NULL, '[]')
                ON CONFLICT(owner_ref, thread_id, task_id, root) DO UPDATE SET
                    last_event_id = excluded.last_event_id,
                    last_edit_at = NULL,
                    changed_paths_json = '[]'
                """,
                (context.owner_ref, context.thread_id, context.task_id, evidence.root, event_id),
            )
            self._prune(conn, context=context, root=evidence.root)
            conn.commit()
        return {"id": event_id, "created_at": created_at, **asdict(context), **asdict(evidence)}

    # LLM: edits retain the prior event pointer and advance last_edit_at; status
    # can therefore say stale instead of silently forgetting what once passed.
    # 函数用途: 文件成功修改后登记路径，让较早的测试结果立即过期。
    def mark_edited(
        self,
        context: VerificationContext,
        *,
        root: str | Path,
        paths: list[str],
    ) -> dict[str, Any]:
        root_text = str(Path(root).expanduser().resolve(strict=False))
        edited_at = _utc_now()
        changed = sorted({str(path) for path in paths if str(path).strip()})
        with _DB_LOCK, self._connection() as conn:
            row = conn.execute(
                """
                SELECT changed_paths_json FROM verification_state
                WHERE owner_ref = ? AND thread_id = ? AND task_id = ? AND root = ?
                """,
                (context.owner_ref, context.thread_id, context.task_id, root_text),
            ).fetchone()
            previous = _json_string_list(row["changed_paths_json"] if row is not None else "[]")
            merged = sorted(set(previous) | set(changed))[-200:]
            conn.execute(
                """
                INSERT INTO verification_state(
                    owner_ref, thread_id, task_id, root, last_event_id, last_edit_at, changed_paths_json
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(owner_ref, thread_id, task_id, root) DO UPDATE SET
                    last_edit_at = excluded.last_edit_at,
                    changed_paths_json = excluded.changed_paths_json
                """,
                (
                    context.owner_ref,
                    context.thread_id,
                    context.task_id,
                    root_text,
                    edited_at,
                    json.dumps(merged, ensure_ascii=False),
                ),
            )
            conn.commit()
        return {**asdict(context), "root": root_text, "last_edit_at": edited_at, "changed_paths": merged}

    # LLM: this projection preserves scope/status verbatim.  Callers must not
    # turn targeted into full or failed/stale into passed.
    # 函数用途: 读取某个任务在某个项目里的最新验证状态。
    def status(self, context: VerificationContext, *, root: str | Path) -> dict[str, Any]:
        root_text = str(Path(root).expanduser().resolve(strict=False))
        with _DB_LOCK, self._connection() as conn:
            state = conn.execute(
                """
                SELECT last_event_id, last_edit_at, changed_paths_json
                FROM verification_state
                WHERE owner_ref = ? AND thread_id = ? AND task_id = ? AND root = ?
                """,
                (context.owner_ref, context.thread_id, context.task_id, root_text),
            ).fetchone()
            if state is None:
                return self._empty_status(context, root_text)
            event = (
                conn.execute("SELECT * FROM verification_events WHERE id = ?", (state["last_event_id"],)).fetchone()
                if state["last_event_id"] is not None
                else None
            )
        changed_paths = _json_string_list(state["changed_paths_json"])
        if event is None:
            return {**self._empty_status(context, root_text), "changed_paths": changed_paths}
        evidence = dict(event)
        status = (
            "stale"
            if state["last_edit_at"] and str(state["last_edit_at"]) > str(evidence["created_at"])
            else str(evidence["status"])
        )
        return {
            "status": status,
            "evidence": evidence,
            **asdict(context),
            "root": root_text,
            "changed_paths": changed_paths,
        }

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema(conn)
        return conn

    # LLM: 每次操作独占一条连接：提交或回滚后立刻关闭，不交给 GC。3.11 起 sqlite3 连接要等循环回收才关，
    #   WAL checkpoint 和 -wal/-shm 删除会拖到任意时刻，操作返回后证据库文件仍在变。调用方仍须持 _DB_LOCK。
    # 函数用途: 打开证据库连接，事务结束后关闭它，让每次读写返回时落盘状态已确定。
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                owner_ref TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                root TEXT NOT NULL,
                command TEXT NOT NULL,
                canonical_command TEXT NOT NULL,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                output_summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_state (
                owner_ref TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                root TEXT NOT NULL,
                last_event_id INTEGER,
                last_edit_at TEXT,
                changed_paths_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY(owner_ref, thread_id, task_id, root)
            );
            CREATE INDEX IF NOT EXISTS idx_verification_events_task_root
            ON verification_events(owner_ref, thread_id, task_id, root, id DESC);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )

    def _prune(self, conn: sqlite3.Connection, *, context: VerificationContext, root: str) -> None:
        conn.execute(
            """
            DELETE FROM verification_events
            WHERE owner_ref = ? AND thread_id = ? AND task_id = ? AND root = ?
              AND id NOT IN (
                SELECT id FROM verification_events
                WHERE owner_ref = ? AND thread_id = ? AND task_id = ? AND root = ?
                ORDER BY id DESC LIMIT ?
              )
              AND id NOT IN (SELECT last_event_id FROM verification_state WHERE last_event_id IS NOT NULL)
            """,
            (
                context.owner_ref,
                context.thread_id,
                context.task_id,
                root,
                context.owner_ref,
                context.thread_id,
                context.task_id,
                root,
                _MAX_EVENTS_PER_TASK_ROOT,
            ),
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_MAX_EVIDENCE_AGE_DAYS)).isoformat()
        conn.execute(
            """
            DELETE FROM verification_events
            WHERE created_at < ?
              AND id NOT IN (SELECT last_event_id FROM verification_state WHERE last_event_id IS NOT NULL)
            """,
            (cutoff,),
        )
        conn.execute(
            """
            DELETE FROM verification_events
            WHERE id NOT IN (SELECT id FROM verification_events ORDER BY id DESC LIMIT ?)
              AND id NOT IN (SELECT last_event_id FROM verification_state WHERE last_event_id IS NOT NULL)
            """,
            (_MAX_TOTAL_UNREFERENCED_EVENTS,),
        )

    @staticmethod
    def _empty_status(context: VerificationContext, root: str) -> dict[str, Any]:
        return {
            "status": "unverified",
            "evidence": None,
            **asdict(context),
            "root": root,
            "changed_paths": [],
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_string_list(value: object) -> list[str]:
    try:
        payload = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if str(item).strip()]


__all__ = ["VerificationContext", "VerificationEvidence", "VerificationEvidenceRepository"]
