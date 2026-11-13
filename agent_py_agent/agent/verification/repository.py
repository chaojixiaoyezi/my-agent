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

from .requirement_provenance import (
    AcceptanceRecord,
    IndependentReviewRecord,
    ObservationScopeHonestyError,
    RequirementProvenanceRecord,
    SelfReviewForbiddenError,
    StaleInputVersionError,
    normalize_path,
    path_intersects,
    validate_independent_review,
    validate_scope_upgrade,
)

_SCHEMA_VERSION = 2
# 证据文件最多接受的天数；超过视为过期证据，恢复时不再采用。
_MAX_EVIDENCE_AGE_DAYS = 30
_MAX_EVENTS_PER_TASK_ROOT = 100
_MAX_TOTAL_UNREFERENCED_EVENTS = 10_000
_DB_LOCK = threading.RLock()


class ProvenanceAuthorizationError(PermissionError):
    """此前获准的候选或回执绑定已变化；不披露新的私有绑定。"""

    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


def _require_expected_requirement(conn, context, expected):
    row = conn.execute('SELECT * FROM requirement_links WHERE owner_ref=? AND requirement_id=?',
        (context.owner_ref, expected.requirement_id)).fetchone()
    actual = _row_to_requirement_record(row) if row is not None else None
    # 审核/验收进展不是候选绑定；同候选并行审核可以继续，但换owner、工作、根、版本、作者或观察路径即撤权。
    fields = ('owner_ref', 'requirement_id', 'task_id', 'root', 'input_version',
              'watched_paths', 'author_principal_id', 'created_at')
    if (expected.owner_ref != context.owner_ref or actual is None
            or any(getattr(actual, name) != getattr(expected, name) for name in fields)):
        raise ProvenanceAuthorizationError('REVIEW_CANDIDATE_CHANGED',
            '原授权候选绑定已变化；本次未读取新绑定或写入审核，请在原工作范围重新核验，禁止沿旧授权盲目重试。')
    return actual


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


# LLM: 验证事实只归一个 owner SQLite；元数据读回不建库、不新增事件，任何 passed/full 都不能提升为任务完成授权。
# 类用途: 保存实际验证和文件修改事实，为上下文与 Goal 提供精确范围的证据引用。
def _owner_verification_high_watermark(conn, owner_ref):
    row = conn.execute('SELECT id FROM verification_events WHERE owner_ref=? ORDER BY id DESC LIMIT 1',
                       (owner_ref,)).fetchone()
    return int(row[0]) if row is not None else 0


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
    # 同时定向失效监控这些已修改路径的需求项（ORCH-05）。
    # 函数用途: 文件成功修改后登记路径，让较早的测试结果立即过期，并精准失效相交需求项。
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
            invalidated = self._invalidate_requirements_on_paths(
                conn,
                owner_ref=context.owner_ref,
                root=root_text,
                paths=changed,
                now_text=edited_at,
            )
            conn.commit()
        return {
            **asdict(context),
            "root": root_text,
            "last_edit_at": edited_at,
            "changed_paths": merged,
            "invalidated_requirements": invalidated,
        }

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

    # LLM: 只投影原状态指向的同 owner/thread/task 验证；只读连接不创建库或迁移，不加载命令、输出和路径。
    # 函数用途: 为 Goal 完成来源提供验证事件编号和结果；证据 scope 不等于整个目标已通过验收。
    def task_metadata(self, context: VerificationContext) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT e.id, e.status, e.scope, e.created_at, s.last_edit_at
                   FROM verification_state s JOIN verification_events e ON e.id = s.last_event_id
                   WHERE s.owner_ref = ? AND s.thread_id = ? AND s.task_id = ?
                     AND e.owner_ref = s.owner_ref AND e.thread_id = s.thread_id AND e.task_id = s.task_id
                   ORDER BY e.id""",
                (context.owner_ref, context.thread_id, context.task_id),
            ).fetchall()
            return [
                {
                    "event_id": row["id"],
                    "scope": row["scope"],
                    "status": "stale"
                    if row["last_edit_at"] and row["last_edit_at"] > row["created_at"]
                    else row["status"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    # LLM: 连接初始化时配置 WAL 模式、忙等待超时及增量自动清理与日志上限（Rule 10）。
    # 函数用途: 创建并配置 SQLite 数据库物理连接。
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_size_limit = 67108864")
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self._ensure_schema(conn)
            return conn
        except BaseException:
            conn.close()
            raise

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

    # LLM: 增量建表与索引，保持 verification_events & verification_state 与 ORCH-05 三大新表完全正交。
    # 函数用途: 确保数据库存在所需全部数据表及性能索引。
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

            CREATE TABLE IF NOT EXISTS requirement_links (
                owner_ref TEXT NOT NULL,
                requirement_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                root TEXT NOT NULL,
                input_version TEXT NOT NULL,
                watched_paths_json TEXT NOT NULL DEFAULT '[]',
                verification_event_ids_json TEXT NOT NULL DEFAULT '[]',
                observation_scope TEXT NOT NULL DEFAULT 'unverified',
                review_status TEXT NOT NULL DEFAULT 'missing',
                acceptance_status TEXT NOT NULL DEFAULT 'unaccepted',
                invalidation_reason TEXT NOT NULL DEFAULT '',
                author_principal_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner_ref, requirement_id)
            );
            CREATE INDEX IF NOT EXISTS idx_requirement_links_owner_task
            ON requirement_links(owner_ref, task_id);
            CREATE INDEX IF NOT EXISTS idx_requirement_links_owner_root
            ON requirement_links(owner_ref, root);

            CREATE TABLE IF NOT EXISTS independent_reviews (
                review_id TEXT NOT NULL,
                requirement_id TEXT NOT NULL,
                owner_ref TEXT NOT NULL,
                reviewer_principal_id TEXT NOT NULL,
                author_principal_id TEXT NOT NULL,
                reviewed_input_version TEXT NOT NULL,
                verdict TEXT NOT NULL,
                scope TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_ref, review_id)
            );
            CREATE INDEX IF NOT EXISTS idx_independent_reviews_req
            ON independent_reviews(owner_ref, requirement_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS acceptance_records (
                acceptance_id TEXT NOT NULL,
                requirement_id TEXT NOT NULL,
                owner_ref TEXT NOT NULL,
                accepted_by_principal_id TEXT NOT NULL,
                accepted_input_version TEXT NOT NULL,
                verdict TEXT NOT NULL,
                observation_scope TEXT NOT NULL,
                review_id TEXT NOT NULL DEFAULT '',
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_ref, acceptance_id)
            );
            CREATE INDEX IF NOT EXISTS idx_acceptance_records_req
            ON acceptance_records(owner_ref, requirement_id, created_at DESC);
            """
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_verification_events_owner_id ON verification_events(owner_ref,id)')
        # 写事务覆盖判列/ALTER/旧边界回填；跨进程首次打开也不能重复迁移或发表半迁移。
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(requirement_links)')}
        if 'verification_invalidated_event_id' not in columns:
            conn.execute('ALTER TABLE requirement_links ADD COLUMN verification_invalidated_event_id INTEGER NOT NULL DEFAULT 0')
            # 旧失效记录不能在迁移后仅靠重review洗白原测试；有效旧绑定继续原样复用。
            conn.execute("UPDATE requirement_links SET verification_invalidated_event_id=COALESCE("
                "(SELECT MAX(id) FROM verification_events e WHERE e.owner_ref=requirement_links.owner_ref),0) "
                "WHERE review_status='stale' OR acceptance_status='invalidated' OR invalidation_reason<>''")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )

    # LLM: 仅保留各任务最近事件并淘汰超期与孤立记录。
    # 函数用途: 清理过期和超出数量限制的验证历史事件。
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

    # LLM: 默认空状态投影。
    # 函数用途: 构造未验证状态的基础字典。
    @staticmethod
    def _empty_status(context: VerificationContext, root: str) -> dict[str, Any]:
        return {
            "status": "unverified",
            "evidence": None,
            **asdict(context),
            "root": root,
            "changed_paths": [],
        }

    # LLM: 仅对受影响 paths 与 watched_paths 存在交集的需求将状态置为 stale/invalidated，其余互不干扰。
    # 函数用途: 在文件被修改后精准失效监控该路径的需求，保留不相关需求的证据与评审。
    def _invalidate_requirements_on_paths(
        self,
        conn: sqlite3.Connection,
        *,
        owner_ref: str,
        root: str,
        paths: list[str],
        now_text: str,
    ) -> list[str]:
        if not paths:
            return []
        norm_edited = {normalize_path(p, root) for p in paths if str(p).strip()}
        if not norm_edited:
            return []

        rows = conn.execute(
            """
            SELECT requirement_id, root, input_version, watched_paths_json, review_status, acceptance_status
            FROM requirement_links
            WHERE owner_ref = ?
            """,
            (owner_ref,),
        ).fetchall()

        watermark = _owner_verification_high_watermark(conn, owner_ref)
        invalidated: list[str] = []
        for row in rows:
            req_id = row["requirement_id"]
            req_root = row["root"] or root
            watched = _json_string_list(row["watched_paths_json"])
            if not watched:
                continue
            norm_watched = [normalize_path(w, req_root) for w in watched]
            intersects = any(
                any(path_intersects(nw, ne) for ne in norm_edited)
                for nw in norm_watched
            )
            if intersects:
                conn.execute(
                    """
                    UPDATE requirement_links
                    SET review_status = 'stale',
                        acceptance_status = 'invalidated',
                        invalidation_reason = 'WATCHED_PATH_MODIFIED',
                        verification_invalidated_event_id = MAX(verification_invalidated_event_id, ?),
                        updated_at = ?
                    WHERE owner_ref = ? AND requirement_id = ?
                    """,
                    (watermark, now_text, owner_ref, req_id),
                )
                invalidated.append(req_id)
        return invalidated

    # LLM: 外部显式调用路径变更失效入口，支持附加新输入版本校验。
    # 函数用途: 主动使匹配监控路径或版本变更的需求项失效。
    def invalidate_edited_paths(
        self,
        context: VerificationContext,
        *,
        root: str | Path,
        paths: list[str],
        new_input_version: str | None = None,
    ) -> list[str]:
        root_text = str(Path(root).expanduser().resolve(strict=False))
        now_text = _utc_now()
        with _DB_LOCK, self._connection() as conn:
            invalidated = self._invalidate_requirements_on_paths(
                conn,
                owner_ref=context.owner_ref,
                root=root_text,
                paths=paths,
                now_text=now_text,
            )
            if new_input_version is not None and new_input_version.strip():
                rows = conn.execute(
                    "SELECT requirement_id, input_version FROM requirement_links WHERE owner_ref = ?",
                    (context.owner_ref,),
                ).fetchall()
                watermark = _owner_verification_high_watermark(conn, context.owner_ref)
                for r in rows:
                    if r["requirement_id"] in invalidated:
                        continue
                    if r["input_version"] != new_input_version.strip():
                        conn.execute(
                            """
                            UPDATE requirement_links
                            SET review_status = 'stale',
                                acceptance_status = 'invalidated',
                                invalidation_reason = 'INPUT_VERSION_CHANGED',
                                verification_invalidated_event_id = MAX(verification_invalidated_event_id, ?),
                                updated_at = ?
                            WHERE owner_ref = ? AND requirement_id = ?
                            """,
                            (watermark, now_text, context.owner_ref, r["requirement_id"]),
                        )
                        invalidated.append(r["requirement_id"])
            conn.commit()
            return invalidated

    # LLM: 注册或更新需求项绑定关系，若输入版本发生变更则将既有评审与验收自动标为过期失效。
    # 函数用途: 登记需求到监控路径与初始输入版本的关联事实。
    def bind_requirement(
        self,
        context: VerificationContext,
        record: RequirementProvenanceRecord,
    ) -> RequirementProvenanceRecord:
        now_text = _utc_now()
        owner_ref = record.owner_ref or context.owner_ref
        task_id = record.task_id or context.task_id
        watched_json = json.dumps(list(record.watched_paths), ensure_ascii=False)

        with _DB_LOCK, self._connection() as conn:
            existing = conn.execute(
                "SELECT * FROM requirement_links WHERE owner_ref = ? AND requirement_id = ?",
                (owner_ref, record.requirement_id),
            ).fetchone()

            if existing is None:
                created_at = record.created_at or now_text
                updated_at = now_text
                review_status = record.review_status if record.review_status != "missing" else "missing"
                acceptance_status = record.acceptance_status if record.acceptance_status != "unaccepted" else "unaccepted"
                observation_scope = record.observation_scope if record.observation_scope != "unverified" else "unverified"
                invalidation_reason = record.invalidation_reason
                events_json = json.dumps(list(record.verification_event_ids), ensure_ascii=False)
                author = record.author_principal_id
                conn.execute(
                    """
                    INSERT INTO requirement_links(
                        owner_ref, requirement_id, task_id, root, input_version,
                        watched_paths_json, verification_event_ids_json, observation_scope,
                        review_status, acceptance_status, invalidation_reason, author_principal_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_ref,
                        record.requirement_id,
                        task_id,
                        record.root,
                        record.input_version,
                        watched_json,
                        events_json,
                        observation_scope,
                        review_status,
                        acceptance_status,
                        invalidation_reason,
                        author,
                        created_at,
                        updated_at,
                    ),
                )
            else:
                created_at = existing["created_at"]
                updated_at = now_text
                existing_author = str(existing["author_principal_id"] or "").strip()
                supplied_author = str(record.author_principal_id or "").strip()
                is_version_changed = bool(record.input_version and record.input_version != existing["input_version"])
                if not is_version_changed:
                    if existing_author and supplied_author and existing_author != supplied_author:
                        raise ValueError(
                            "AUTHOR_PRINCIPAL_MISMATCH: cannot overwrite bound author without changing input_version"
                        )
                author = supplied_author or existing_author
                if is_version_changed:
                    conn.execute('UPDATE requirement_links SET verification_invalidated_event_id='
                        'MAX(verification_invalidated_event_id,?) WHERE owner_ref=? AND requirement_id=?',
                        (_owner_verification_high_watermark(conn, owner_ref), owner_ref, record.requirement_id))
                    review_status = "stale"
                    acceptance_status = "invalidated"
                    invalidation_reason = "INPUT_VERSION_CHANGED"
                    observation_scope = existing["observation_scope"]
                    events_json = existing["verification_event_ids_json"]
                else:
                    review_status = record.review_status if record.review_status != "missing" else existing["review_status"]
                    acceptance_status = (
                        record.acceptance_status if record.acceptance_status != "unaccepted" else existing["acceptance_status"]
                    )
                    observation_scope = (
                        record.observation_scope if record.observation_scope != "unverified" else existing["observation_scope"]
                    )
                    invalidation_reason = record.invalidation_reason or existing["invalidation_reason"]
                    events_json = (
                        json.dumps(list(record.verification_event_ids), ensure_ascii=False)
                        if record.verification_event_ids
                        else existing["verification_event_ids_json"]
                    )

                conn.execute(
                    """
                    UPDATE requirement_links
                    SET task_id = ?,
                        root = ?,
                        input_version = ?,
                        watched_paths_json = ?,
                        verification_event_ids_json = ?,
                        observation_scope = ?,
                        review_status = ?,
                        acceptance_status = ?,
                        invalidation_reason = ?,
                        author_principal_id = ?,
                        updated_at = ?
                    WHERE owner_ref = ? AND requirement_id = ?
                    """,
                    (
                        task_id,
                        record.root or existing["root"],
                        record.input_version or existing["input_version"],
                        watched_json,
                        events_json,
                        observation_scope,
                        review_status,
                        acceptance_status,
                        invalidation_reason,
                        author,
                        updated_at,
                        owner_ref,
                        record.requirement_id,
                    ),
                )
            conn.commit()
            updated_row = conn.execute(
                "SELECT * FROM requirement_links WHERE owner_ref = ? AND requirement_id = ?",
                (owner_ref, record.requirement_id),
            ).fetchone()
            return _row_to_requirement_record(updated_row)

    # LLM: 绑定测试命令证据，严格执行范围诚实性：只有存在全量集成事实且具备匹配版本的通过评审才允许置为 full_candidate。
    # 函数用途: 将测试执行结果编号关联至需求项，阻断局部测试虚标全量。
    def link_verification(
        self,
        context: VerificationContext,
        *,
        requirement_id: str,
        event_ids: list[int] | tuple[int, ...],
        observation_scope: str = "module_only",
        input_version: str | None = None,
    ) -> RequirementProvenanceRecord:
        now_text = _utc_now()
        with _DB_LOCK, self._connection() as conn:
            req_row = conn.execute(
                "SELECT * FROM requirement_links WHERE owner_ref = ? AND requirement_id = ?",
                (context.owner_ref, requirement_id),
            ).fetchone()
            if req_row is None:
                raise KeyError(f"Requirement '{requirement_id}' not bound for owner '{context.owner_ref}'")

            current_input_version = req_row["input_version"]
            if input_version is not None and input_version.strip() and input_version.strip() != current_input_version:
                raise StaleInputVersionError(
                    f"STALE_INPUT_VERSION: provided input_version '{input_version}' does not match requirement '{current_input_version}'"
                )

            int_event_ids = [int(eid) for eid in event_ids]
            has_integrated = False
            if observation_scope != "unverified":
                if not int_event_ids:
                    raise ObservationScopeHonestyError(
                        "EMPTY_VERIFICATION_EVENTS: cannot promote observation_scope without verification events"
                    )
                unique_ids = list(set(int_event_ids))
                placeholders = ",".join("?" for _ in unique_ids)
                event_rows = conn.execute(
                    f"SELECT id, root, scope, status FROM verification_events WHERE owner_ref = ? AND id IN ({placeholders})",
                    (context.owner_ref, *unique_ids),
                ).fetchall()
                if len(event_rows) != len(unique_ids):
                    raise ObservationScopeHonestyError(
                        "UNKNOWN_VERIFICATION_EVENT_ID: all linked verification_event_ids must exist for this owner"
                    )
                _require_matching_verification_roots(event_rows, req_row["root"])
                if any(er['id'] <= req_row['verification_invalidated_event_id'] for er in event_rows):
                    raise StaleInputVersionError("STALE_VERIFICATION_EVENT: event predates the requirement invalidation boundary")
                if not all(er["status"] == "passed" for er in event_rows):
                    raise ObservationScopeHonestyError(
                        "FAILED_VERIFICATION_EVENT_LINKED: cannot promote observation_scope using failed verification events"
                    )
                for er in event_rows:
                    if er["scope"] == "full" and er["status"] == "passed":
                        has_integrated = True
                        break
            else:
                if int_event_ids:
                    unique_ids = list(set(int_event_ids))
                    placeholders = ",".join("?" for _ in unique_ids)
                    event_rows = conn.execute(
                        f"SELECT id, root, scope, status FROM verification_events WHERE owner_ref = ? AND id IN ({placeholders})",
                        (context.owner_ref, *unique_ids),
                    ).fetchall()
                    _require_matching_verification_roots(event_rows, req_row["root"])
                    for er in event_rows:
                        if er["scope"] == "full" and er["status"] == "passed":
                            has_integrated = True
                            break

            review_row = conn.execute(
                """
                SELECT review_id, reviewed_input_version, verdict
                FROM independent_reviews
                WHERE owner_ref = ? AND requirement_id = ? AND verdict = 'pass'
                ORDER BY created_at DESC LIMIT 1
                """,
                (context.owner_ref, requirement_id),
            ).fetchone()
            has_matching_approved_review = (
                review_row is not None
                and review_row["reviewed_input_version"] == current_input_version
                and req_row["review_status"] == "approved"
            )

            validate_scope_upgrade(
                observation_scope,
                has_integrated_evidence=has_integrated,
                has_matching_approved_review=has_matching_approved_review,
            )

            existing_events = _json_int_list(req_row["verification_event_ids_json"])
            merged_events = sorted(set(existing_events) | set(int_event_ids))

            conn.execute(
                """
                UPDATE requirement_links
                SET verification_event_ids_json = ?,
                    observation_scope = ?,
                    updated_at = ?
                WHERE owner_ref = ? AND requirement_id = ?
                """,
                (
                    json.dumps(merged_events, ensure_ascii=False),
                    observation_scope,
                    now_text,
                    context.owner_ref,
                    requirement_id,
                ),
            )
            conn.commit()
            updated_row = conn.execute(
                "SELECT * FROM requirement_links WHERE owner_ref = ? AND requirement_id = ?",
                (context.owner_ref, requirement_id),
            ).fetchone()
            return _row_to_requirement_record(updated_row)

    # LLM: 记录独立评审，严格拦截作者自评（SELF_REVIEW_FORBIDDEN），更新评审状态。
    # 函数用途: 登记代码与架构独立评审结论，校验作者与评审人隔离。
    def record_independent_review(
        self,
        context: VerificationContext,
        record: IndependentReviewRecord,
        *,
        expected_requirement: RequirementProvenanceRecord | None = None,
        include_requirement: bool = False,
    ) -> IndependentReviewRecord | tuple[IndependentReviewRecord, RequirementProvenanceRecord]:
        now_text = _utc_now()
        created_at = record.created_at or now_text
        owner_ref = record.owner_ref or context.owner_ref

        with _DB_LOCK, self._connection() as conn:
            if expected_requirement is not None:
                # 同一写事务内复核完整候选绑定；锁外门的旧快照不能授权后来换绑的任务。
                if not conn.in_transaction:
                    conn.execute('BEGIN IMMEDIATE')
                if (owner_ref != context.owner_ref or record.requirement_id != expected_requirement.requirement_id
                        or record.reviewer_principal_id.strip() != context.task_id):
                    raise ProvenanceAuthorizationError('REVIEW_RECEIPT_IDENTITY_MISMATCH',
                        '审核回执身份与原执行者/候选不符；未写入。')
                _require_expected_requirement(conn, context, expected_requirement)
            req_row = conn.execute(
                "SELECT * FROM requirement_links WHERE owner_ref = ? AND requirement_id = ?",
                (owner_ref, record.requirement_id),
            ).fetchone()

            stored_author = str(req_row["author_principal_id"] or "").strip() if req_row is not None else ""
            supplied_author = record.author_principal_id.strip()
            if stored_author and supplied_author and stored_author != supplied_author:
                raise ValueError(
                    f"AUTHOR_PRINCIPAL_MISMATCH: requirement '{record.requirement_id}' is bound to author '{stored_author}', got '{supplied_author}'"
                )
            author = stored_author or supplied_author
            validate_independent_review(record.reviewer_principal_id, author)

            effective_record = IndependentReviewRecord(
                review_id=record.review_id or f"rev-{now_text}",
                requirement_id=record.requirement_id,
                reviewer_principal_id=record.reviewer_principal_id.strip(),
                author_principal_id=author,
                reviewed_input_version=record.reviewed_input_version.strip(),
                verdict=record.verdict,
                scope=record.scope or "independent_review",
                evidence_refs=record.evidence_refs,
                created_at=created_at,
                owner_ref=owner_ref,
            )

            if expected_requirement is not None:
                previous = conn.execute('SELECT * FROM independent_reviews WHERE owner_ref=? AND review_id=?',
                    (owner_ref, effective_record.review_id)).fetchone()
                if previous is not None:
                    saved = _row_to_review_record(previous)
                    fields = ('requirement_id', 'owner_ref', 'reviewer_principal_id', 'author_principal_id',
                              'reviewed_input_version', 'verdict', 'scope', 'evidence_refs')
                    if (any(getattr(saved, name) != getattr(effective_record, name) for name in fields)
                            or record.created_at and record.created_at != saved.created_at):
                        raise ProvenanceAuthorizationError('REVIEW_RECEIPT_CONFLICT',
                            '审核回执编号已有不同身份或内容；旧回执不可篡改，本次未写入，不得借原编号重试覆盖。')
                    return (saved, _row_to_requirement_record(req_row)) if include_requirement else saved

            conn.execute(
                """
                INSERT INTO independent_reviews(
                    review_id, requirement_id, owner_ref, reviewer_principal_id,
                    author_principal_id, reviewed_input_version, verdict, scope,
                    evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_ref, review_id) DO UPDATE SET
                    verdict = excluded.verdict,
                    scope = excluded.scope,
                    evidence_refs_json = excluded.evidence_refs_json,
                    created_at = excluded.created_at
                """,
                (
                    effective_record.review_id,
                    effective_record.requirement_id,
                    effective_record.owner_ref,
                    effective_record.reviewer_principal_id,
                    effective_record.author_principal_id,
                    effective_record.reviewed_input_version,
                    effective_record.verdict,
                    effective_record.scope,
                    json.dumps(list(effective_record.evidence_refs), ensure_ascii=False),
                    effective_record.created_at,
                ),
            )

            if req_row is not None:
                current_ver = req_row["input_version"]
                if effective_record.verdict == "pass":
                    if effective_record.reviewed_input_version == current_ver:
                        new_review_status = "approved"
                    else:
                        new_review_status = "stale"
                else:
                    new_review_status = "changes_requested"

                conn.execute(
                    """
                    UPDATE requirement_links
                    SET review_status = ?,
                        invalidation_reason = CASE WHEN ? = 'approved' THEN '' ELSE invalidation_reason END,
                        author_principal_id = CASE WHEN author_principal_id != '' THEN author_principal_id ELSE ? END,
                        updated_at = ?
                    WHERE owner_ref = ? AND requirement_id = ?
                    """,
                    (new_review_status, new_review_status, author, now_text, owner_ref, effective_record.requirement_id),
                )
            current = (conn.execute('SELECT * FROM requirement_links WHERE owner_ref=? AND requirement_id=?',
                (owner_ref, record.requirement_id)).fetchone() if include_requirement else None)
            conn.commit()
            return (effective_record, _row_to_requirement_record(current)) if include_requirement else effective_record

    # LLM: 记录验收决定，校验输入版本与评审通过状态，防止基于过期或未审核事实验收。
    # 函数用途: 登记最终或阶段验收事实并切换需求验收状态为 accepted。
    def record_acceptance(
        self,
        context: VerificationContext,
        record: AcceptanceRecord,
    ) -> AcceptanceRecord:
        now_text = _utc_now()
        created_at = record.created_at or now_text
        owner_ref = record.owner_ref or context.owner_ref

        with _DB_LOCK, self._connection() as conn:
            req_row = conn.execute(
                "SELECT * FROM requirement_links WHERE owner_ref = ? AND requirement_id = ?",
                (owner_ref, record.requirement_id),
            ).fetchone()
            if req_row is None:
                raise KeyError(f"Requirement '{record.requirement_id}' not bound for owner '{owner_ref}'")

            current_ver = req_row["input_version"]
            if record.accepted_input_version.strip() != current_ver:
                raise StaleInputVersionError(
                    f"STALE_INPUT_VERSION: accepted input version '{record.accepted_input_version}' != requirement version '{current_ver}'"
                )

            effective_verdict = (record.verdict or "accepted").strip()
            if effective_verdict not in {"accepted", "rejected", "changes_requested"}:
                raise ValueError(f"INVALID_ACCEPTANCE_VERDICT: '{effective_verdict}'")

            stored_author = str(req_row["author_principal_id"] or "").strip()
            if stored_author:
                validate_independent_review(record.accepted_by_principal_id, stored_author)

            if effective_verdict == "accepted":
                if req_row["review_status"] != "approved":
                    raise ValueError(
                        f"REQUIREMENT_NOT_APPROVED: cannot record acceptance when review_status is '{req_row['review_status']}'"
                    )

                if req_row["acceptance_status"] == "invalidated" and req_row["invalidation_reason"]:
                    raise ValueError(
                        f"REQUIREMENT_INVALIDATED: requirement has been invalidated ({req_row['invalidation_reason']})"
                    )

            scope = record.observation_scope or req_row["observation_scope"]
            scopes = {"unverified": 0, "module_only": 1, "focused_integration": 2, "full_candidate": 3}
            if scope not in scopes or req_row["observation_scope"] not in scopes:
                raise ObservationScopeHonestyError("INVALID_OBSERVATION_SCOPE: unknown structured observation scope")
            if scopes[scope] > scopes[req_row["observation_scope"]]:
                raise ObservationScopeHonestyError(
                    "ACCEPTANCE_SCOPE_EXCEEDS_VERIFIED_SCOPE: link the matching verification evidence before accepting this scope")
            review_id = str(record.review_id or "").strip()
            review = None
            if review_id:
                review = conn.execute(
                    "SELECT * FROM independent_reviews WHERE owner_ref=? AND requirement_id=? AND review_id=?",
                    (owner_ref, record.requirement_id, review_id)).fetchone()
                if review is None:
                    raise ValueError("REVIEW_REFERENCE_NOT_FOUND: review_id must identify this owner's exact requirement review")
            elif effective_verdict == "accepted":
                # 旧调用可省略引用，但必须冻结实际匹配的通过记录，不能再保存空review_id。
                review = conn.execute(
                    "SELECT * FROM independent_reviews WHERE owner_ref=? AND requirement_id=? "
                    "AND reviewed_input_version=? AND verdict='pass' ORDER BY created_at DESC,review_id DESC LIMIT 1",
                    (owner_ref, record.requirement_id, current_ver)).fetchone()
                if review is None:
                    raise ValueError("REVIEW_REFERENCE_NOT_FOUND: no passing review matches the accepted input version")
                review_id = review["review_id"]
            if review is not None:
                if review["reviewed_input_version"] != current_ver:
                    raise StaleInputVersionError("STALE_REVIEW_REFERENCE: review_id belongs to another input version")
                if effective_verdict == "accepted" and review["verdict"] != "pass":
                    raise ObservationScopeHonestyError("REVIEW_NOT_APPROVED: acceptance requires the referenced review to pass")
                if review["author_principal_id"] != stored_author:
                    raise ValueError("REVIEW_AUTHOR_MISMATCH: review does not match the requirement author")
                validate_independent_review(review["reviewer_principal_id"], stored_author)
            if effective_verdict == "accepted" and scope != "unverified":
                event_ids = [identity for identity in _json_int_list(req_row["verification_event_ids_json"])
                             if identity > req_row["verification_invalidated_event_id"]]
                missing_code = "FULL_CANDIDATE_VERIFICATION_MISSING" if scope == "full_candidate" else "VERIFICATION_MISSING"
                if not event_ids:
                    raise ObservationScopeHonestyError(f"{missing_code}: no current linked verification events")
                placeholders = ','.join('?' for _ in event_ids)
                events = conn.execute(
                    f"SELECT id,root,status,scope FROM verification_events WHERE owner_ref=? AND id IN ({placeholders})",
                    (owner_ref, *event_ids)).fetchall()
                if len(events) != len(set(event_ids)):
                    raise ObservationScopeHonestyError(f"{missing_code}: linked verification event is unavailable")
                _require_matching_verification_roots(events, req_row["root"])
                if not all(event['status'] == 'passed' for event in events):
                    raise ObservationScopeHonestyError("VERIFICATION_NOT_PASSED: current linked verification must have passed")
                if scope == "full_candidate":
                    validate_scope_upgrade(scope,
                        has_integrated_evidence=any(event['scope'] == 'full' for event in events),
                        has_matching_approved_review=review is not None and review['verdict'] == 'pass')

            effective_record = AcceptanceRecord(
                acceptance_id=record.acceptance_id or f"acc-{now_text}",
                requirement_id=record.requirement_id,
                accepted_by_principal_id=record.accepted_by_principal_id.strip(),
                accepted_input_version=record.accepted_input_version.strip(),
                verdict=effective_verdict,
                observation_scope=scope,
                review_id=review_id,
                evidence_refs=record.evidence_refs,
                created_at=created_at,
                owner_ref=owner_ref,
            )

            conn.execute(
                """
                INSERT INTO acceptance_records(
                    acceptance_id, requirement_id, owner_ref, accepted_by_principal_id,
                    accepted_input_version, verdict, observation_scope, review_id,
                    evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_ref, acceptance_id) DO UPDATE SET
                    verdict = excluded.verdict,
                    observation_scope = excluded.observation_scope,
                    review_id = excluded.review_id,
                    evidence_refs_json = excluded.evidence_refs_json,
                    created_at = excluded.created_at
                """,
                (
                    effective_record.acceptance_id,
                    effective_record.requirement_id,
                    effective_record.owner_ref,
                    effective_record.accepted_by_principal_id,
                    effective_record.accepted_input_version,
                    effective_record.verdict,
                    effective_record.observation_scope,
                    effective_record.review_id,
                    json.dumps(list(effective_record.evidence_refs), ensure_ascii=False),
                    effective_record.created_at,
                ),
            )

            new_acceptance_status = "accepted" if effective_verdict == "accepted" else "rejected"
            conn.execute(
                """
                UPDATE requirement_links
                SET acceptance_status = ?,
                    invalidation_reason = '',
                    updated_at = ?
                WHERE owner_ref = ? AND requirement_id = ?
                """,
                (new_acceptance_status, now_text, owner_ref, effective_record.requirement_id),
            )
            conn.commit()
            return effective_record

    # LLM: 按 owner 与 requirement_id 读取单条需求溯源快照。
    # 函数用途: 查询单个需求的详细溯源记录。
    def get_requirement(self, owner_ref: str, requirement_id: str) -> RequirementProvenanceRecord | None:
        with _DB_LOCK, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM requirement_links WHERE owner_ref = ? AND requirement_id = ?",
                (owner_ref, requirement_id),
            ).fetchone()
            return _row_to_requirement_record(row) if row is not None else None

    def read_authorized_provenance(self, context, expected):
        """同一只读快照复核原绑定并读取有界历史，不跨连接返回后来换绑的私有内容。"""
        with _DB_LOCK, self._connection() as conn:
            # 初始化仅含 schema/meta；先提交，再为候选与历史建立同一读快照。
            conn.commit()
            conn.execute('BEGIN')
            requirement = _require_expected_requirement(conn, context, expected)
            params = (context.owner_ref, expected.requirement_id)
            reviews = conn.execute('SELECT * FROM independent_reviews WHERE owner_ref=? AND requirement_id=? '
                'ORDER BY created_at DESC LIMIT 100', params).fetchall()
            acceptances = conn.execute('SELECT * FROM acceptance_records WHERE owner_ref=? AND requirement_id=? '
                'ORDER BY created_at DESC LIMIT 100', params).fetchall()
            return ([requirement], [_row_to_review_record(row) for row in reviews],
                [_row_to_acceptance_record(row) for row in acceptances])

    # LLM: 分页查询某 owner 下需求绑定列表（有界 limit 防深扫）。
    # 函数用途: 列出匹配任务或目录的所有需求溯源项。
    def list_requirements(
        self,
        owner_ref: str,
        *,
        task_id: str | None = None,
        root: str | None = None,
        limit: int = 100,
    ) -> list[RequirementProvenanceRecord]:
        limit_bounded = min(max(1, int(limit or 100)), 200)
        query = "SELECT * FROM requirement_links WHERE owner_ref = ?"
        params: list[Any] = [owner_ref]
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if root:
            norm_root = str(Path(root).expanduser().resolve(strict=False))
            query += " AND (root = ? OR root = '')"
            params.append(norm_root)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit_bounded)

        with _DB_LOCK, self._connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_requirement_record(r) for r in rows]

    # LLM: 分页查询某需求或 owner 的独立评审历史记录。
    # 函数用途: 列出满足条件的独立评审记录列表。
    def list_independent_reviews(
        self,
        owner_ref: str,
        *,
        requirement_id: str | None = None,
        limit: int = 100,
    ) -> list[IndependentReviewRecord]:
        limit_bounded = min(max(1, int(limit or 100)), 200)
        query = "SELECT * FROM independent_reviews WHERE owner_ref = ?"
        params: list[Any] = [owner_ref]
        if requirement_id:
            query += " AND requirement_id = ?"
            params.append(requirement_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit_bounded)

        with _DB_LOCK, self._connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_review_record(r) for r in rows]

    # LLM: 分页查询某需求或 owner 的验收事实历史。
    # 函数用途: 列出满足条件的验收事实记录列表。
    def list_acceptance_records(
        self,
        owner_ref: str,
        *,
        requirement_id: str | None = None,
        limit: int = 100,
    ) -> list[AcceptanceRecord]:
        limit_bounded = min(max(1, int(limit or 100)), 200)
        query = "SELECT * FROM acceptance_records WHERE owner_ref = ?"
        params: list[Any] = [owner_ref]
        if requirement_id:
            query += " AND requirement_id = ?"
            params.append(requirement_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit_bounded)

        with _DB_LOCK, self._connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_acceptance_record(r) for r in rows]


# LLM: 生成当前 UTC ISO 时间戳字符串。
# 函数用途: 获取当前时间。
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM: 安全解析 JSON 字符串数组，返回纯净字符串列表。
# 函数用途: 反序列化 JSON 文本为字符串列表。
def _json_string_list(value: object) -> list[str]:
    try:
        payload = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if str(item).strip()]


# LLM: 安全解析 JSON 整数数组。
# 函数用途: 反序列化 JSON 文本为整型数字列表。
def _json_int_list(value: object) -> list[int]:
    try:
        payload = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    result: list[int] = []
    for item in payload:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


# LLM: 将数据库 Row 投影为 RequirementProvenanceRecord。
# 函数用途: 从 SQLite 查询行构建需求记录。
def _row_to_requirement_record(row: sqlite3.Row) -> RequirementProvenanceRecord:
    watched = tuple(_json_string_list(row["watched_paths_json"]))
    event_ids = tuple(_json_int_list(row["verification_event_ids_json"]))
    return RequirementProvenanceRecord(
        requirement_id=row["requirement_id"],
        owner_ref=row["owner_ref"],
        task_id=row["task_id"],
        root=row["root"],
        input_version=row["input_version"],
        watched_paths=watched,
        verification_event_ids=event_ids,
        verification_invalidated_event_id=int(row["verification_invalidated_event_id"]),
        observation_scope=row["observation_scope"],
        review_status=row["review_status"],
        acceptance_status=row["acceptance_status"],
        invalidation_reason=row["invalidation_reason"],
        author_principal_id=row["author_principal_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# LLM: 将数据库 Row 投影为 IndependentReviewRecord。
# 函数用途: 从 SQLite 查询行构建独立评审记录。
def _row_to_review_record(row: sqlite3.Row) -> IndependentReviewRecord:
    refs = tuple(_json_string_list(row["evidence_refs_json"]))
    return IndependentReviewRecord(
        review_id=row["review_id"],
        requirement_id=row["requirement_id"],
        reviewer_principal_id=row["reviewer_principal_id"],
        author_principal_id=row["author_principal_id"],
        reviewed_input_version=row["reviewed_input_version"],
        verdict=row["verdict"],
        scope=row["scope"],
        evidence_refs=refs,
        created_at=row["created_at"],
        owner_ref=row["owner_ref"],
    )


# LLM: 将数据库 Row 投影为 AcceptanceRecord。
# 函数用途: 从 SQLite 查询行构建验收事实记录。
def _row_to_acceptance_record(row: sqlite3.Row) -> AcceptanceRecord:
    refs = tuple(_json_string_list(row["evidence_refs_json"]))
    return AcceptanceRecord(
        acceptance_id=row["acceptance_id"],
        requirement_id=row["requirement_id"],
        accepted_by_principal_id=row["accepted_by_principal_id"],
        accepted_input_version=row["accepted_input_version"],
        verdict=row["verdict"],
        observation_scope=row["observation_scope"],
        review_id=row["review_id"],
        evidence_refs=refs,
        created_at=row["created_at"],
        owner_ref=row["owner_ref"],
    )


def _require_matching_verification_roots(rows, requirement_root: str) -> None:
    """同 owner 不等于同项目；测试回执必须属于需求所绑定的规范根。"""
    expected = Path(requirement_root).expanduser().resolve(strict=False)
    for row in rows:
        if not row["root"] or Path(row["root"]).expanduser().resolve(strict=False) != expected:
            raise ObservationScopeHonestyError(
                f"VERIFICATION_ROOT_MISMATCH: evidence {row['id']} belongs to another project root"
            )


__all__ = ["VerificationContext", "VerificationEvidence", "VerificationEvidenceRepository"]
