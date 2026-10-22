# LLM: 共享due索引的claim和全局修复只归C；父类拒门覆盖W adapter，原owner sync/alias和Count0算法保持。
# 模块用途: 投影owner定时任务并供协调者领取到期owner，不给W全局扫描或迁移权限。
from __future__ import annotations

"""Process-wide due-owner projection for owner-local scheduler ledgers.

Owner ``store.json`` files remain the only scheduler authority.  This SQLite
index contains only the owner identity and its earliest wake time so a gateway
restart can find due owners without walking every tenant directory.  Every
claimed owner is revalidated against its owner ledger before any run is
reserved or executed.
"""

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import Any

from ..storage_backend import ensure_sqlite_wal

_SCHEMA_VERSION = 1
_SCHEMA_INIT_LOCK = threading.Lock()
_SCHEMA_INITIALIZED_DBS: OrderedDict[str, tuple[int, int, int]] = OrderedDict()
_SCHEMA_INITIALIZED_MAX = 256
_ACTIVE_RUN_STATUSES = frozenset({"queued", "claimed", "running", "waiting"})
_LEGACY_SCAN_KEY = "owner_scheduler_ledgers_v1"


class SchedulerDueIndexError(RuntimeError):
    """The scheduler projection cannot be read or updated safely."""


@dataclass(frozen=True)
class DueOwner:
    provider: str
    owner_kind: str
    owner_id: str
    next_due_at: float


@dataclass(frozen=True)
class _LegacyOwnerLedger:
    provider: str
    owner_kind: str
    owner_id: str
    path: Path


# LLM: 到期唤醒全局 SQLite 索引投影，权威 job 状态仍在 owner 账本；按物理文件指纹缓存 schema 初始化，改动须同步 test_scheduler_due_index.py。
# 类用途: 维护跨 owner 的最近到期时刻索引与监控短编号分配，供网关后台按时唤醒目标 owner。
class SchedulerDueIndex:
    """SQLite projection used only to wake the correct owner at the right time."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # LLM: 展示编号使用原全局 SQLite 的原子分配器，唯一 job 仍在 owner 账；丢弃的预留允许留空号，不回收编号。
    # 函数用途: 为同一部署的监控分配不冲突的 job01 编号，让跨群/私聊汇总可直接使用。
    def allocate_job_alias(self) -> str:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS scheduler_job_numbers (number INTEGER PRIMARY KEY AUTOINCREMENT)")
            cursor = conn.execute("INSERT INTO scheduler_job_numbers DEFAULT VALUES")
            number = cursor.lastrowid
            conn.commit()
        return f"job{number:02d}"

    # LLM: 索引仍非执行权威；新时间相等或有效租约内向后推进不得清租，更早工作才能提前重新领取。
    # 函数用途: 更新 owner 到期投影，同时保留未过期且不阻挡更早工作的领取租约。
    def sync_owner(
        self,
        owner: dict[str, object],
        store: dict[str, object],
        *,
        now: float | None = None,
    ) -> float:
        provider, owner_kind, owner_id = _owner_key(owner)
        current = float(time.time() if now is None else now)
        next_due_at = _earliest_due_at(store, now=current)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if next_due_at > 0:
                    conn.execute(
                        """
                        INSERT INTO scheduler_due_owners (
                            provider, owner_kind, owner_id, next_due_at, lease_until, updated_at
                        ) VALUES (?, ?, ?, ?, 0, ?)
                        ON CONFLICT(provider, owner_kind, owner_id) DO UPDATE SET
                            next_due_at = excluded.next_due_at,
                            lease_until = CASE
                                WHEN scheduler_due_owners.next_due_at = excluded.next_due_at
                                THEN scheduler_due_owners.lease_until
                                WHEN scheduler_due_owners.lease_until > ?
                                     AND excluded.next_due_at >= scheduler_due_owners.next_due_at
                                THEN scheduler_due_owners.lease_until
                                ELSE 0
                            END,
                            updated_at = excluded.updated_at
                        WHERE excluded.updated_at >= scheduler_due_owners.updated_at
                        """,
                        (provider, owner_kind, owner_id, next_due_at, current, current),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM scheduler_due_owners
                        WHERE provider = ? AND owner_kind = ? AND owner_id = ?
                        """,
                        (provider, owner_kind, owner_id),
                    )
                conn.commit()
        except (OSError, sqlite3.Error) as exc:
            raise SchedulerDueIndexError("scheduler due index update failed") from exc
        return next_due_at

    # LLM: 到期owner全局领取只归C；守原父类公开门使W adapter及直接父类均拒，sync_owner/alias受控业务保持。
    # 函数用途: 在共享调度领取事务前拒W，不改变排序、租期或owner自身投影更新。
    def claim_due_owners(
        self,
        *,
        now: float | None = None,
        limit: int = 8,
        lease_seconds: float = 30.0,
    ) -> list[DueOwner]:
        from ..common.global_write_guard import require_global_writer

        require_global_writer("scheduler_due_claim")
        current = float(time.time() if now is None else now)
        selected_limit = max(1, min(int(limit or 1), 1024))
        lease_until = current + max(1.0, float(lease_seconds or 1.0))
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT provider, owner_kind, owner_id, next_due_at
                    FROM scheduler_due_owners
                    WHERE next_due_at <= ? AND lease_until <= ?
                    ORDER BY next_due_at, provider, owner_kind, owner_id
                    LIMIT ?
                    """,
                    (current, current, selected_limit),
                ).fetchall()
                if rows:
                    conn.executemany(
                        """
                        UPDATE scheduler_due_owners
                        SET lease_until = ?
                        WHERE provider = ? AND owner_kind = ? AND owner_id = ?
                        """,
                        [
                            (lease_until, row["provider"], row["owner_kind"], row["owner_id"])
                            for row in rows
                        ],
                    )
                conn.commit()
        except (OSError, sqlite3.Error) as exc:
            raise SchedulerDueIndexError("scheduler due index claim failed") from exc
        return [
            DueOwner(
                provider=str(row["provider"]),
                owner_kind=str(row["owner_kind"]),
                owner_id=str(row["owner_id"]),
                next_due_at=float(row["next_due_at"]),
            )
            for row in rows
        ]

    # LLM: 全局旧账扫描/修复只归C；拒门先于扫描和SQLite事务，不影响W原existing-only构造及owner更新。
    # 函数用途: 阻止W修复共享到期索引，保留Count0原迁移标记和错误计数。
    def repair_legacy_owner_ledgers(self, owners_dir: str | Path) -> dict[str, int | bool]:
        """One-time projection rebuild for jobs created before this index existed."""
        from ..common.global_write_guard import require_global_writer

        require_global_writer("scheduler_due_repair")

        if self._migration_complete(_LEGACY_SCAN_KEY):
            return {"completed": True, "scanned": 0, "indexed": 0, "errors": 0}
        try:
            rows, scanned, errors = _load_legacy_due_rows(owners_dir)
        except OSError as exc:
            raise SchedulerDueIndexError("scheduler due index legacy scan failed") from exc
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    """
                    INSERT INTO scheduler_due_owners (
                        provider, owner_kind, owner_id, next_due_at, lease_until, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(provider, owner_kind, owner_id) DO UPDATE SET
                        next_due_at = excluded.next_due_at,
                        lease_until = CASE
                            WHEN scheduler_due_owners.next_due_at = excluded.next_due_at
                            THEN scheduler_due_owners.lease_until
                            ELSE 0
                        END,
                        updated_at = excluded.updated_at
                    WHERE excluded.updated_at >= scheduler_due_owners.updated_at
                    """,
                    rows,
                )
                if errors == 0:
                    # An unreadable owner ledger cannot be silently forgotten.
                    # Leave the migration unlatched so the next gateway restart
                    # retries after an operator repairs that owner store.
                    conn.execute(
                        """
                        INSERT INTO scheduler_due_meta (key, value, updated_at)
                        VALUES (?, 'complete', ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at
                        """,
                        (_LEGACY_SCAN_KEY, time.time()),
                    )
                conn.commit()
        except (OSError, sqlite3.Error) as exc:
            raise SchedulerDueIndexError("scheduler due index legacy repair failed") from exc
        return {
            "completed": errors == 0,
            "scanned": scanned,
            "indexed": len(rows),
            "errors": errors,
        }

    def snapshot(self) -> list[DueOwner]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT provider, owner_kind, owner_id, next_due_at
                    FROM scheduler_due_owners
                    ORDER BY next_due_at, provider, owner_kind, owner_id
                    """
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise SchedulerDueIndexError("scheduler due index read failed") from exc
        return [
            DueOwner(
                provider=str(row["provider"]),
                owner_kind=str(row["owner_kind"]),
                owner_id=str(row["owner_id"]),
                next_due_at=float(row["next_due_at"]),
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # LLM: 缓存只绑定实际验证的文件代次；换代有界重试且不发布旧成功；短连接始终close，不遗留WAL锁。
    # 函数用途: 热构造只stat不连接SQLite；进程冷启动验证已有表，新库才执行DDL与版本写入。
    def _ensure_schema(self) -> None:
        try:
            with _SCHEMA_INIT_LOCK:
                key, identity = str(self.path), _schema_file_key(self.path)
                if identity is not None and _SCHEMA_INITIALIZED_DBS.get(key) == identity:
                    _SCHEMA_INITIALIZED_DBS.move_to_end(key)
                    return
                _SCHEMA_INITIALIZED_DBS.pop(key, None)
                for _attempt in range(3):
                    identity = self._initialize_current_schema()
                    if identity is None:
                        continue
                    _SCHEMA_INITIALIZED_DBS[key] = identity
                    _SCHEMA_INITIALIZED_DBS.move_to_end(key)
                    while len(_SCHEMA_INITIALIZED_DBS) > _SCHEMA_INITIALIZED_MAX:
                        _SCHEMA_INITIALIZED_DBS.popitem(last=False)
                    return
                raise SchedulerDueIndexError("scheduler due index changed during initialization")
        except (OSError, sqlite3.Error) as exc:
            raise SchedulerDueIndexError("scheduler due index initialization failed") from exc

    # LLM: 连接前后及关闭后均核对物理文件身份；换代只返回None交调用方有界重试，不发布缓存或旧成功。
    # 函数用途: 用必关闭的短连接验证/初始化当前库，返回可以安全发布的schema文件代次。
    def _initialize_current_schema(self) -> tuple[int, int, int] | None:
        before = _schema_file_key(self.path, allow_empty=True)
        with closing(self._connect()) as conn, conn:
            connected = _schema_file_key(self.path, allow_empty=True)
            if connected is None or (before is not None and connected != before):
                return None
            initialized = (conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
                           and conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                            "AND name IN ('scheduler_due_owners', 'scheduler_due_meta')").fetchone()[0] == 2)
            ensure_sqlite_wal(conn)
            if not initialized:
                _create_due_schema(conn)
        after = _schema_file_key(self.path)
        return after if after == connected else None

    def _migration_complete(self, key: str) -> bool:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM scheduler_due_meta WHERE key = ?",
                    (key,),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise SchedulerDueIndexError("scheduler due index migration state read failed") from exc
        return bool(row is not None and str(row["value"]) == "complete")


# LLM: 仅非空普通文件可命中schema缓存；空文件身份仅用于初始化前后代次核对，不按业务mtime重复建表。
# 函数用途: 取得SQLite文件的设备/inode/版本键，缺失或空文件交给原初始化路径。
def _schema_file_key(path: Path, *, allow_empty: bool = False) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return ((int(st.st_dev), int(st.st_ino), _SCHEMA_VERSION)
            if S_ISREG(st.st_mode) and (allow_empty or st.st_size > 0) else None)


# LLM: 只在版本/表验证未通过时执行原schema定义；不改owner权限、业务表内容或claim协议。
# 函数用途: 初始化原due表、元数据表、到期索引和schema版本。
def _create_due_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS scheduler_due_owners (
        provider TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
        next_due_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL, PRIMARY KEY (provider, owner_kind, owner_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scheduler_due_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS scheduler_due_owner_ready_idx
        ON scheduler_due_owners (next_due_at, lease_until)""")
    conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")


def _load_legacy_due_rows(
    owners_dir: str | Path,
) -> tuple[list[tuple[str, str, str, float, float]], int, int]:
    ledgers = _discover_legacy_owner_ledgers(owners_dir)
    rows: list[tuple[str, str, str, float, float]] = []
    errors = 0
    for ledger in ledgers:
        row, failed = _read_legacy_due_row(ledger)
        if failed:
            errors += 1
        elif row is not None:
            rows.append(row)
    return rows, len(ledgers), errors


def _discover_legacy_owner_ledgers(owners_dir: str | Path) -> list[_LegacyOwnerLedger]:
    ledgers: list[_LegacyOwnerLedger] = []
    for provider_dir in _child_directories(Path(owners_dir) / "providers"):
        ledgers.extend(_provider_legacy_owner_ledgers(provider_dir))
    return ledgers


def _provider_legacy_owner_ledgers(provider_dir: Path) -> list[_LegacyOwnerLedger]:
    return [
        *_bucket_legacy_owner_ledgers(provider_dir, "users", "user"),
        *_bucket_legacy_owner_ledgers(provider_dir, "groups", "group"),
    ]


def _bucket_legacy_owner_ledgers(
    provider_dir: Path,
    bucket: str,
    owner_kind: str,
) -> list[_LegacyOwnerLedger]:
    ledgers: list[_LegacyOwnerLedger] = []
    for owner_home in _child_directories(provider_dir / bucket):
        path = owner_home / "data" / "scheduler" / "store.json"
        if path.is_file():
            ledgers.append(
                _LegacyOwnerLedger(
                    provider=provider_dir.name,
                    owner_kind=owner_kind,
                    owner_id=owner_home.name,
                    path=path,
                )
            )
    return ledgers


def _read_legacy_due_row(
    ledger: _LegacyOwnerLedger,
) -> tuple[tuple[str, str, str, float, float] | None, bool]:
    try:
        store = json.loads(ledger.path.read_text(encoding="utf-8"))
        if not isinstance(store, dict):
            raise ValueError("scheduler store must be an object")
        updated_at = _positive_timestamp(store.get("updated_at")) or ledger.path.stat().st_mtime
        next_due_at = _earliest_due_at(store, now=updated_at)
    except (OSError, UnicodeError, ValueError):
        return None, True
    if next_due_at <= 0:
        return None, False
    return (
        ledger.provider,
        ledger.owner_kind,
        ledger.owner_id,
        next_due_at,
        updated_at,
    ), False


def _owner_key(owner: dict[str, object]) -> tuple[str, str, str]:
    provider = str(owner.get("provider") or "").strip()
    owner_kind = str(owner.get("kind") or "").strip()
    owner_id = str(owner.get("id") or "").strip()
    if not provider or not owner_kind or not owner_id:
        raise SchedulerDueIndexError("scheduler due index owner identity is incomplete")
    return provider, owner_kind, owner_id


# LLM: 活动 job/rule 不再重复投影旧周期；queued 按派发、claimed/running 按租约唤醒，waiting 由原会话续接。
# 函数用途: 合并可运行规则与活动进程的真实关注时间，避免等待阶段用 now 重复唤醒 owner。
def _earliest_due_at(store: dict[str, object], *, now: float) -> float:
    candidates: list[float] = []
    raw_runs = store.get("runs")
    runs = [raw for raw in raw_runs.values() if isinstance(raw, dict)
            and str(raw.get("status") or "") in _ACTIVE_RUN_STATUSES] if isinstance(raw_runs, dict) else []
    active_jobs, active_rules = _validated_active_keys(store, runs)
    jobs = store.get("jobs")
    if isinstance(jobs, dict):
        for job_id, raw in jobs.items():
            if not isinstance(raw, dict) or str(raw.get("status") or "") != "active":
                continue
            candidates.extend(_job_due_times(raw, str(raw.get("job_id") or job_id), active_jobs, active_rules, now))
    for raw in runs:
        status = str(raw.get("status") or "")
        if status == "waiting":
            continue
        field = "dispatch_after" if status == "queued" else "claim_expires_at"
        candidates.append(_positive_timestamp(raw.get(field)) or now)
    return min(candidates) if candidates else 0.0


# LLM: 只借用原纯解析器，不构造 Repository 或访问目录；坏 schema/owner/epoch 等记录不得排除合法周期。
# 函数用途: 让索引的活动运行阻挡口径与原 owner 账本完全一致，避免坏 waiting 导致漏唤醒。
def _validated_active_keys(store: dict, runs: list[dict]) -> tuple[set[str], set[tuple[str, str]]]:
    from .repository import _SchedulerRecordParsing

    owner = store.get("owner")
    if not isinstance(owner, dict):
        return set(), set()
    parser = _SchedulerRecordParsing()
    parser.owner = owner
    valid = [run for raw in runs if (run := parser._parse_run(raw)[0]) is not None]
    return ({str(run["job_id"]) for run in valid},
            {(str(run["job_id"]), str(run.get("rule_id") or "")) for run in valid})


# LLM: 周期规则同时服从原预约的父 job 时间/退避门槛；活动运行按 job/rule 排除，事件 pending 仍按原事件入口投影。
# 函数用途: 合并未被占用规则的父子有效到期时间，避免旧顶层退避引起空轮取锁。
def _job_due_times(job: dict, job_id: str, active_jobs: set[str], active_rules: set[tuple[str, str]], now: float) -> list[float]:
    from .event_sources import event_due_times

    rules = job.get("rules")
    if isinstance(rules, list):
        available = [rule for rule in rules if isinstance(rule, dict) and rule.get("enabled")
                     and (job_id, str(rule.get("id") or "")) not in active_rules]
        job = {**job, "rules": available}
    elif job_id in active_jobs:
        return []
    else:
        available = [job]
    job_next = _positive_timestamp(job.get("next_run_at"))
    gate = max(job_next, _positive_timestamp(job.get("backoff_until")))
    due = [max(gate, value, _positive_timestamp(rule.get("backoff_until"))) for rule in available
           if job_next > 0 and (value := _positive_timestamp(rule.get("next_run_at"))) > 0]
    return [*due, *event_due_times(job, now)]


def _positive_timestamp(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _child_directories(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [path for path in root.iterdir() if path.is_dir() and not path.is_symlink()]


__all__ = [
    "DueOwner",
    "SchedulerDueIndex",
    "SchedulerDueIndexError",
]
