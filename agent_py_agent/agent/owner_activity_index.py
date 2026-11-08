"""Cell 级可重建活动索引；NULL 是未知/无期限，投影不授予执行或休眠权。"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .storage_backend import ensure_sqlite_wal

_DEADLINES = {"runtime_wake": "next_wake_at", "scheduler_due": "next_job_due_at", "maintenance": "next_maintenance_at"}
_REVISION_LOCK = threading.Lock()
_REVISION = time.time_ns()


def next_hint_revision() -> int:
    """在提交后的通知叶分配一次修订，不用墙钟倒退或旧行时间当版本。"""
    global _REVISION
    with _REVISION_LOCK:
        _REVISION += 1
        return _REVISION


def owner_key(owner) -> tuple[str, str, str]:
    if isinstance(owner, tuple) and len(owner) == 3:
        return tuple(str(value) for value in owner)
    if isinstance(owner, dict):
        return str(owner["provider"]), str(owner.get("kind", owner.get("owner_kind"))), str(owner.get("id", owner.get("owner_id")))
    return owner.provider, owner.owner_kind, owner.owner_id


@dataclass(frozen=True)
class IdleReport:
    idle: bool
    unknown_sources: tuple[str, ...]
    earliest_deadline: float | None


class OwnerActivityIndex:
    """持久化期限和事实；活跃计数仅从锁外的现时probe读取。"""

    def __init__(self, path):
        global _REVISION
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._probe_lock = threading.Lock()
        self._probes = {}
        with closing(self._connect()) as conn, conn:
            ensure_sqlite_wal(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS owner_activity (
                provider TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
                next_wake_at REAL, next_job_due_at REAL, next_maintenance_at REAL,
                hard_facts INTEGER, soft_facts INTEGER,
                facts_revision INTEGER NOT NULL DEFAULT 0, facts_checked_at REAL,
                last_activity_at REAL, PRIMARY KEY(provider, owner_kind, owner_id))""")
            # 各kind独立CAS；不能让较新的job通知吞掉较旧但尚未发表的runtime通知。
            conn.execute("""CREATE TABLE IF NOT EXISTS deadline_revisions (
                provider TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
                kind TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(provider, owner_kind, owner_id, kind))""")
            conn.execute("CREATE INDEX IF NOT EXISTS owner_activity_checked ON owner_activity(facts_checked_at)")
            conn.execute("""CREATE INDEX IF NOT EXISTS owner_activity_unknown
                ON owner_activity(provider,owner_kind,owner_id)
                WHERE hard_facts IS NULL OR soft_facts IS NULL OR facts_checked_at IS NULL""")
            for column in _DEADLINES.values():
                conn.execute(f"CREATE INDEX IF NOT EXISTS owner_activity_{column} ON owner_activity({column})")
            persisted = conn.execute("SELECT MAX(revision) FROM (SELECT MAX(facts_revision) AS revision FROM owner_activity "
                "UNION ALL SELECT MAX(revision) FROM deadline_revisions)").fetchone()[0]
        # 重启恰遇墙钟回退时，也不能用较低的进程序号覆盖已持久化修订。
        with _REVISION_LOCK:
            _REVISION = max(_REVISION, int(persisted or 0))

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def note_deadline(self, owner, kind, due_wall: float | None, *, revision: int):
        column = _DEADLINES[kind]
        if due_wall is not None and not math.isfinite(due_wall):
            raise ValueError("deadline must be finite or None")
        key = owner_key(owner)
        with closing(self._connect()) as conn, conn:
            changed = conn.execute("""INSERT INTO deadline_revisions VALUES(?,?,?,?,?)
                ON CONFLICT(provider,owner_kind,owner_id,kind) DO UPDATE SET revision=excluded.revision
                WHERE excluded.revision > deadline_revisions.revision""", (*key, kind, revision)).rowcount
            if changed:
                conn.execute(f"""INSERT INTO owner_activity(provider,owner_kind,owner_id,{column}) VALUES(?,?,?,?)
                    ON CONFLICT(provider,owner_kind,owner_id) DO UPDATE SET {column}=excluded.{column}""", (*key, due_wall))

    def note_facts(self, owner, hard: bool | None, soft: bool | None, *, revision: int):
        with closing(self._connect()) as conn, conn:
            conn.execute("""INSERT INTO owner_activity(provider,owner_kind,owner_id,hard_facts,soft_facts,facts_revision,facts_checked_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,owner_kind,owner_id) DO UPDATE SET
                hard_facts=excluded.hard_facts,soft_facts=excluded.soft_facts,
                facts_revision=excluded.facts_revision,facts_checked_at=excluded.facts_checked_at
                WHERE excluded.facts_revision > owner_activity.facts_revision""",
                (*owner_key(owner), hard, soft, revision, time.time()))

    def note_activity(self, owner, at):
        with closing(self._connect()) as conn, conn:
            conn.execute("""INSERT INTO owner_activity(provider,owner_kind,owner_id,last_activity_at) VALUES(?,?,?,?)
                ON CONFLICT(provider,owner_kind,owner_id) DO UPDATE SET
                last_activity_at=MAX(COALESCE(last_activity_at,excluded.last_activity_at),excluded.last_activity_at)""",
                (*owner_key(owner), at))

    def row(self, owner):
        key = owner_key(owner)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM owner_activity WHERE provider=? AND owner_kind=? AND owner_id=?", key).fetchone()
            if row is None:
                return None
            revisions = conn.execute("SELECT kind,revision FROM deadline_revisions WHERE provider=? AND owner_kind=? AND owner_id=?", key).fetchall()
        return {**dict(row), "deadline_revisions": {row["kind"]: row["revision"] for row in revisions}}

    def priority_owners(self, *, now, facts_before, limit):
        """只取有界候选；缺失和过期事实优先，不能把未知排成安静owner。"""
        limit = max(1, int(limit))
        frontiers = [
            ("hard_facts IS NULL OR soft_facts IS NULL OR facts_checked_at IS NULL",
             "provider,owner_kind,owner_id", ()),
            ("facts_checked_at<=?", "facts_checked_at", (facts_before,)),
            *((f"{column}<=?", column, (now,)) for column in _DEADLINES.values()),
        ]
        candidates = {}
        with closing(self._connect()) as conn:
            # 各前沿沿索引直接取页，避免 LIMIT 前全表 OR 扫描与排序。
            for predicate, ordering, parameters in frontiers:
                rows = conn.execute(
                    f"SELECT provider,owner_kind,owner_id FROM owner_activity WHERE {predicate} "
                    f"ORDER BY {ordering} LIMIT ?", (*parameters, limit))
                candidates.update((tuple(row), None) for row in rows)
        return list(candidates)[:limit]

    def forget_owner(self, owner):
        """只清失效投影，不删除任何owner权威文件。"""
        key = owner_key(owner)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM owner_activity WHERE provider=? AND owner_kind=? AND owner_id=?", key)
            conn.execute("DELETE FROM deadline_revisions WHERE provider=? AND owner_kind=? AND owner_id=?", key)

    def register_activity_probe(self, name, fn):
        with self._probe_lock:
            self._probes[name] = fn

    def idle_report(self, owner, *, wake_margin_seconds, idle_seconds, now) -> IdleReport:
        row = self.row(owner) or {}
        unknown = [name for name in ("hard_facts", "soft_facts", "last_activity_at") if row.get(name) is None]
        deadlines = [row[column] for column in _DEADLINES.values() if row.get(column) is not None]
        earliest = min(deadlines) if deadlines else None
        with self._probe_lock:
            probes = dict(self._probes)
        counts = []
        for name in sorted(set(probes) | {"inflight", "streams", "processes"}):
            try:
                value = probes[name](owner)
            except Exception:
                value = None
            if type(value) is not int or value < 0:
                unknown.append(name)
            else:
                counts.append(value)
        idle = (not unknown and not any(counts) and not row.get("hard_facts") and not row.get("soft_facts")
                and (earliest is None or earliest > now + wake_margin_seconds)
                and row["last_activity_at"] < now - idle_seconds)
        return IdleReport(bool(idle), tuple(unknown), earliest)

    def rebuild(self, owner_iter):
        """按调用方给的有界页补投影，允许重启续页；不清除并发通知刚发表的期限。"""
        for item in owner_iter:
            owner, data = item if len(item) == 2 and isinstance(item[1], dict) else (item, {})
            revision = data.get("facts_revision", next_hint_revision())
            if data:
                self.note_facts(owner, data.get("hard_facts"), data.get("soft_facts"), revision=revision)
                for kind, column in _DEADLINES.items():
                    if column in data:
                        self.note_deadline(owner, kind, data[column], revision=revision)
                if data.get("last_activity_at") is not None:
                    self.note_activity(owner, data["last_activity_at"])
            else:
                with closing(self._connect()) as conn, conn:
                    conn.execute("INSERT OR IGNORE INTO owner_activity(provider,owner_kind,owner_id) VALUES(?,?,?)", owner_key(owner))
