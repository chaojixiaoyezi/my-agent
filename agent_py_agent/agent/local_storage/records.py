
# LLM: 派生记录共用原写入/审计权威，索引比较只取必要列及流式正文，不另建记录或持久状态。
# LLM: C仅增加原scope本地IO分支，默认body→SQL/FTS/event顺序和身份不变；联验LocalStore/源撤权及partial测试。
# 模块用途: 管理原派生记录与正文，C末写消费同一次活来源。
from __future__ import annotations

"""owns LocalStore record upsert, lookup, content-file paths, and row hydration.

这个文件只管'记录本身'。
新增/更新一条记录、按 ID 读取、把数据库行变成搜索结果、正文文件放在哪，都归这里。
"""

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    write_private_text_file_atomic,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import open_readonly_file_beneath, unlink_file_beneath
from .models import PREVIEW_CHARS, LocalSearchResult

DERIVED_PRUNABLE_SOURCE_TYPES: frozenset[str] = frozenset({
    "subagent_dispatch", "subagent_work_log", "subagent_channel_probe",
    "subagent_runner_result", "subagent_run", "subagent_execution_context",
    "subagent_capability_route", "gateway_event", "gateway_request",
})
DERIVED_RECORD_TTL_SECONDS: float = 30.0 * 86400.0
_DERIVED_TERMINAL_STATUSES = frozenset({
    "done", "completed", "succeeded", "failed", "cancelled", "abandoned", "timeout",
    "timed_out", "channel_error", "stopped", "dead_lettered", "ok", "error", "taken_over",
})
_DERIVED_PENDING_FILES_KEY = "derived_prune_pending_files"
_DERIVED_FILE_CLEANUP_MAX_FILES = 1000
_DERIVED_FILE_CLEANUP_MAX_BYTES = 8 * 1024 * 1024


# LLM: 只认结构化终态与通知事实；未知/缺状态不推断结束，BLOCKED明确不在终态集合。
# 函数用途: 判断派生快照已结束且没有待收口、待通知工作。
def derived_record_terminal(metadata: dict[str, Any]) -> bool:
    status = metadata.get("status")
    return (isinstance(status, str) and status.strip().casefold() in _DERIVED_TERMINAL_STATUSES
            and not _derived_notification_pending(metadata))


# LLM: 未完成标志或不认识的通知状态均保留；无通知字段表示没有这类挂起事实。
# 函数用途: 保住待收口、唤醒和未送达的完成通知。
def _derived_notification_pending(metadata):
    flags = ("runtime_closeout_pending", "completion_notice_pending", "notification_pending",
             "wake_pending", "completion_delivery_pending", "pending_completion_notification")
    if any(metadata.get(key) for key in flags):
        return True
    for key in ("notification_status", "completion_notification_status", "completion_delivery_status"):
        if key in metadata and (not isinstance(metadata[key], str) or metadata[key] not in
                                {"sent", "delivered", "consumed", "done", "completed", "acknowledged"}):
            return True
    return metadata.get("completion_notified") is False


# LLM: 0是旧结构的缺省时间而非1970年终态；bool、非有限数和坏时间不能赋予清理资格。
# 函数用途: 读取可信的正数Unix时间。
def _derived_timestamp(value):
    try:
        number = float(value) if not isinstance(value, bool) else 0.0
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


# LLM: 请求/任务重建共用30天口径；采用所有有效时间的最新者，文件mtime仅在载荷没有有效时间时兜底。
# 函数用途: 判断可信终态是否已离开重建保留窗，不改变归档本身。
def derived_terminal_expired(metadata, cutoff, fallback_timestamp=None):
    if not derived_record_terminal(metadata):
        return False
    values = [metadata.get(key) for key in ("ended_at", "updated_at", "completed_at", "created_at")]
    if any(isinstance(value, bool) or (value not in (None, 0, "") and _derived_timestamp(value) is None) for value in values):
        return False
    stamps = [stamp for value in values if (stamp := _derived_timestamp(value)) is not None]
    latest = max(stamps) if stamps else _derived_timestamp(fallback_timestamp)
    return latest is not None and latest < cutoff


@dataclass(frozen=True)
class LocalRecordInput:
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any] | None = None
    visibility: str = "private"
    record_id: object | None = None


@dataclass(frozen=True)
class LocalRecordLogInput:
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any] | None = None
    visibility: str = "private"
    record_id: object | None = None
    event_type: str = "local_record_logged"


@dataclass(frozen=True)
class _PreparedRecord:
    record_id: str
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    metadata_json: str
    visibility: str
    content_hash: str
    stored_path: str
    now: float


# LLM: 文件流只读一块字符并精确比实际内容；保留errors=replace及尾部长度判断，不靠数据库hash替代内容验证。
# 函数用途: 不水合整份正文地检查索引文件内容是否一致。
def _content_stream_matches(handle, content):
    position = 0
    while chunk := handle.read(65536):
        if chunk != content[position:position + len(chunk)]:
            return False
        position += len(chunk)
    return position == len(content)


# LLM: 比较与水合共用原路径及UTF8/preview语义，读取模式不同不能让坏正文被误认一致。
# 类用途: 提供记录的路径解析、精确比较及完整读取辅助。
class _LocalStoreRecordHelpers:
    # LLM: 只取比较需要的列；hash仅作快速否定，正文仍按原UTF8替换解码逐块精确比较，缺文件仍用原preview。
    # 函数用途: 判断派生索引的正文和元数据是否已一致，不水合整条记录或保留第二份正文。
    def record_matches(self, record: LocalRecordInput) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                'SELECT content_hash, metadata_json, content_path, content_preview FROM records WHERE id = ?',
                (record.record_id,),
            ).fetchone()
        if row is None:
            return False
        if row['content_hash'] != hashlib.sha256(record.content.encode('utf-8')).hexdigest():
            return False
        return (json.loads(row['metadata_json'] or '{}') == (record.metadata or {})
                and self._content_matches(row, record.content))

    # LLM: 原_read_content的文件存在/preview及errors=replace语义不变；逐块比实际字符，不能仅相信hash而漏掉坏正文文件。
    # 函数用途: 验证派生正文与canonical投影完全一致，内存只多一块，不生成完整返回正文。
    def _content_matches(self, row, content: str) -> bool:
        path = self._resolve_content_path(row['content_path'])
        if not path.exists():
            return row['content_preview'] == content
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            return _content_stream_matches(handle, content)

    def _row_to_result(self, row: sqlite3.Row, *, score: float = 0.0) -> LocalSearchResult:
        metadata = json.loads(row["metadata_json"] or "{}")
        return LocalSearchResult(
            id=row["id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            title=row["title"],
            content=self._read_content(row),
            metadata=metadata,
            visibility=row["visibility"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            score=score,
            content_path=row["content_path"],
        )

    def _read_content(self, row: sqlite3.Row) -> str:
        path = self._resolve_content_path(row["content_path"])
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return row["content_preview"]

    def _content_file(self, record_id: str) -> Path:
        return self.files_dir / f"{record_id}.txt"

    def _stored_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _resolve_content_path(self, stored_path: str) -> Path:
        path = Path(stored_path)
        if path.is_absolute():
            return path
        return self.root / path


# LLM: 派生正文与SQLite记录共享原schema/连接工厂；删除只清精确canonical正文，错误必须向恢复层传播。
# LLM: coordinator_io只是策略；不存source/callback，C与默认共用原SQL/identity/返回叶。
# 类用途: 写入或擦除原派生记录，保留正文先写的真实partial。
class _LocalStoreRecordWriter(_LocalStoreRecordHelpers):
    # LLM: 默认仍原正文→SQL/FTS/event→commit，C只走明确本地事务端口；结果与identity同步回归。
    # LLM: 正文先写的upsert与TTL提交后unlink共持数据库级派生文件锁，避免删掉尚未提交的新正文；锁等待不进入C末门。
    # 函数用途: 新增或更新同一原记录，C每个末写须当前原scope。
    def upsert_record(
        self,
        params: LocalRecordInput | None = None,
        *,
        source_type: str | None = None,
        source_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        visibility: str = "private",
        record_id: object | None = None,
    ) -> LocalSearchResult:

        params = params or LocalRecordInput(
            source_type=str(source_type),
            source_id=str(source_id),
            title=str(title),
            content=str(content),
            metadata=metadata,
            visibility=visibility,
            record_id=record_id,
        )
        return _upsert_record_serialized(self, params)

    # LLM: 默认write_content=True保原先写body时点；C仅将原字段计算提前，在固定叶写同body，不改hash/ID。
    # 函数用途: 准备原记录参数，默认同时原子写正文，C明确分开准备与落盘。
    def _prepare_record(self, params: LocalRecordInput, *, write_content: bool = True) -> _PreparedRecord:
        clean_source_type = params.source_type.strip() or "unknown"
        clean_source_id = params.source_id.strip() or str(uuid.uuid4())
        clean_title = params.title.strip() or clean_source_id
        clean_record_id = params.record_id or self.make_record_id(clean_source_type, clean_source_id)
        content_path = self._content_file(clean_record_id)
        # 原子写:blob 内容半写即损坏(体检实锤),统一走私有原子文本写：目录缺失按 0700 建、文件 0600。
        if write_content:
            write_private_text_file_atomic(content_path, params.content)
        metadata = params.metadata or {}
        return _PreparedRecord(
            record_id=clean_record_id,
            source_type=clean_source_type,
            source_id=clean_source_id,
            title=clean_title,
            content=params.content,
            metadata=metadata,
            metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            visibility=str(params.visibility),
            content_hash=hashlib.sha256(params.content.encode("utf-8")).hexdigest(),
            stored_path=self._stored_path(content_path),
            now=time.time(),
        )

    def _existing_created_at(self, conn: sqlite3.Connection, record_id: str, now: float) -> float:
        old = conn.execute("SELECT created_at FROM records WHERE id = ?", (record_id,)).fetchone()
        return float(old["created_at"]) if old else now

    def _upsert_record_row(self, conn: sqlite3.Connection, record: _PreparedRecord, created_at: float) -> None:
        conn.execute(
            """
            INSERT INTO records (
                id, source_type, source_id, title, content_path, content_hash,
                content_preview, metadata_json, visibility, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_type=excluded.source_type,
                source_id=excluded.source_id,
                title=excluded.title,
                content_path=excluded.content_path,
                content_hash=excluded.content_hash,
                content_preview=excluded.content_preview,
                metadata_json=excluded.metadata_json,
                visibility=excluded.visibility,
                updated_at=excluded.updated_at
            """,
            (
                record.record_id,
                record.source_type,
                record.source_id,
                record.title,
                record.stored_path,
                record.content_hash,
                record.content[:PREVIEW_CHARS],
                record.metadata_json,
                record.visibility,
                created_at,
                record.now,
            ),
        )

    # LLM: 原事件构造/载荷只在_record_event一处；C仅透传同events_path已持锁append叶，默认不加keyword。
    # 函数用途: 登记原upsert事件，不重建第二事件算法。
    def _record_upsert_event(self, conn: sqlite3.Connection, record: _PreparedRecord, *, append_records=None) -> None:
        self._record_event(
            conn,
            "record_upserted",
            record.record_id,
            {
                "source_type": record.source_type,
                "source_id": record.source_id,
                "title": record.title,
                "visibility": record.visibility,
                "content_hash": record.content_hash,
            },
            **({} if append_records is None else {"append_records": append_records}),
        )

    # LLM: 删除派生记录时先清空外置正文，再在一笔 SQLite 事务中删 records/FTS 并写无正文审计。
    # LLM: C显式分支保持先擦body→完整SQL→unlink/checkpoint，每个实际末叶消费当前原source。
    # 函数用途: 移除同一派生记录，unlink失败仍保先擦正文的原契约。
    def delete_record(self, record_id: str) -> bool:
        """Delete one derived record and scrub its externalized content file."""

        clean_id = str(record_id or "").strip()
        if not clean_id:
            return False
        if self.coordinator_io:
            return self._delete_record_coordinator(clean_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT content_path, content_hash, source_type, source_id FROM records WHERE id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            return self._delete_orphan_content(clean_id)

        content_path = self._resolve_content_path(str(row["content_path"] or ""))
        # Wipe first. If unlink later fails, no plaintext remains in the file.
        write_private_text_file_atomic(content_path, "")
        with self._connection() as conn:
            conn.execute("PRAGMA secure_delete=ON")
            if self.fts_available:
                conn.execute("DELETE FROM records_fts WHERE id = ?", (clean_id,))
            conn.execute("DELETE FROM records WHERE id = ?", (clean_id,))
            self._record_event(
                conn,
                "record_deleted",
                clean_id,
                {
                    "source_type": str(row["source_type"] or ""),
                    "source_id": str(row["source_id"] or ""),
                    "content_hash": str(row["content_hash"] or ""),
                },
            )
            conn.commit()
        try:
            content_path.unlink(missing_ok=True)
        except OSError:
            pass
        checkpoint = getattr(self, "_truncate_redacted_wal", None)
        if callable(checkpoint):
            checkpoint()
        return True


    # LLM: 无SQL行时只检查规范单组件ID对应的正文文件，不相信外部路径；擦除失败向上传递以便恢复重试。
    # LLM: C只把原wipe/unlink放当前末门，默认与原单组件ID检查不变；不接任意路径。
    # 函数用途: 擦除原canonical孤立正文，失败保原异常与恢复事实。
    def _delete_orphan_content(self, record_id: str) -> bool:
        if record_id in {".", ".."} or any(char in record_id for char in ("/", "\\", "\x00")):
            return False
        path = self._content_file(record_id)
        if not path.exists() and not path.is_symlink():
            return False
        if self.coordinator_io:
            self._write_coordinator_content(path, "")
        else:
            write_private_text_file_atomic(path, "")
        try:
            if self.coordinator_io:
                self._local_write(lambda: path.unlink(missing_ok=True))
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        return True


    # LLM: 原返回查询/fallback只抽为共用叶，字段与get_record调用时点不变，不新建权威或写盘。
    # 函数用途: 在原SQL提交后返回同一记录，保持原查询失败后的数据形状。
    def _prepared_record_result(self, prepared, created_at):
        return self.get_record(prepared.record_id) or LocalSearchResult(
            id=prepared.record_id,
            source_type=prepared.source_type,
            source_id=prepared.source_id,
            title=prepared.title,
            content=prepared.content,
            metadata=prepared.metadata,
            visibility=prepared.visibility,
            created_at=created_at,
            updated_at=prepared.now,
            content_path=prepared.stored_path,
        )


    # LLM: 原body锁在Root外且FD出生逐次核源；实际原子写仅原路径/文本，C一次replace不等PermissionError。
    # 函数用途: 在C的固定本地末门写一条原正文或擦除空串，不缓存活来源。
    def _write_coordinator_content(self, path, content):
        with locked_json_path(path, birth_commit=self._local_write):
            self._local_write(lambda: write_private_text_file_atomic_unlocked(
                path, content, replace_once=True))

    # LLM: 调用方已持派生命名空间锁，替代upsert原单正文锁；同样一次FD出生、正文、event锁、SQL、读回的原scope相序不变。
    # 函数用途: 按C零等待IO策略执行同一upsert算法，失败保原body先写partial。
    def _upsert_record_coordinator(self, params):
        prepared = self._prepare_record(params, write_content=False)
        self._local_write(lambda: write_private_text_file_atomic_unlocked(
            self._content_file(prepared.record_id), prepared.content, replace_once=True))

        # LLM: 仅在已持事件锁的原短window内调用原SQL叶，不含正文读、网络或等待。
        # 函数用途: 完成原记录/FTS/事件的单库事务内容。
        def operation(conn):
            created_at = self._existing_created_at(conn, prepared.record_id, prepared.now)
            self._upsert_record_row(conn, prepared, created_at)
            if self.fts_available:
                self._replace_fts_row(conn, prepared.record_id, prepared.title, prepared.content)
            self._record_upsert_event(conn, prepared, append_records=self._append_coordinator_event_records)
            return created_at
        with self._coordinator_events_lock():
            created_at = self._coordinator_transaction(operation)
        return self._prepared_record_result(prepared, created_at)

    # LLM: 原行读/先擦body/SQL删除/commit/unlink/checkpoint相序保持；每个本地末叶重用当次scope，不能新源补偿。
    # 函数用途: 为C删除同一派生记录并保原隐私擦除与失败事实。
    def _delete_record_coordinator(self, clean_id):
        with self._connection() as conn:
            row = conn.execute(
                "SELECT content_path, content_hash, source_type, source_id FROM records WHERE id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            return self._delete_orphan_content(clean_id)
        content_path = self._resolve_content_path(str(row["content_path"] or ""))
        self._write_coordinator_content(content_path, "")

        # LLM: 正文已经擦除；同一原本地事务内删FTS/record并记事件，异常回滚SQL且不伪恢复body。
        # 函数用途: 完成C删除的原SQL和无正文事件。
        def operation(conn):
            if self.fts_available:
                conn.execute("DELETE FROM records_fts WHERE id = ?", (clean_id,))
            conn.execute("DELETE FROM records WHERE id = ?", (clean_id,))
            self._record_event(conn, "record_deleted", clean_id, {
                "source_type": str(row["source_type"] or ""),
                "source_id": str(row["source_id"] or ""),
                "content_hash": str(row["content_hash"] or ""),
            }, append_records=self._append_coordinator_event_records)
        with self._coordinator_events_lock():
            self._coordinator_transaction(operation, secure_delete=True)
        try:
            self._local_write(lambda: content_path.unlink(missing_ok=True))
        except OSError:
            pass
        checkpoint = getattr(self, "_truncate_redacted_wal", None)
        if callable(checkpoint):
            self._local_write(checkpoint)
        return True


class _LocalStoreRecordLogger:
    def log_record(
        self,
        params: LocalRecordLogInput | None = None,
        *,
        source_type: str | None = None,
        source_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        visibility: str = "private",
        record_id: object | None = None,
        event_type: str = "local_record_logged",
    ) -> LocalSearchResult:

        params = params or LocalRecordLogInput(
            source_type=str(source_type),
            source_id=str(source_id),
            title=str(title),
            content=str(content),
            metadata=metadata,
            visibility=visibility,
            record_id=record_id,
            event_type=event_type,
        )
        metadata = params.metadata or {}
        record = self.upsert_record(
            LocalRecordInput(
                source_type=params.source_type,
                source_id=params.source_id,
                title=params.title,
                content=params.content,
                metadata=metadata,
                visibility=params.visibility,
                record_id=params.record_id,
            )
        )
        self.record_event(
            str(params.event_type),
            record_id=record.id,
            payload={
                "source_type": params.source_type,
                "source_id": params.source_id,
                "title": params.title,
                **metadata,
            },
        )
        return record

    def get_record(self, record_id: str) -> LocalSearchResult | None:
        """按 ID 读取一条记录。"""

        with self._connection() as conn:
            row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return self._row_to_result(row) if row else None

    @staticmethod
    def make_record_id(source_type: str, source_id: str) -> str:
        """按来源生成稳定记录 ID。"""

        digest = hashlib.sha256(f"{source_type}\0{source_id}".encode()).hexdigest()
        return f"rec-{digest[:24]}"


class LocalStoreRecordMixin(_LocalStoreRecordWriter, _LocalStoreRecordLogger):
    """LocalStore record write, lookup, and log APIs."""

    # LLM: 仅白名单的无引用可信终态可删；调用方须传入当前Compact/恢复/记忆证据引用，不在热路径自动执行。
    # 函数用途: 分批移除30天外的派生归档；dry_run纯查询，正文只在提交后安全清理。
    def prune_derived_records(
        self, *, before: float | None = None, ttl_seconds: float = DERIVED_RECORD_TTL_SECONDS,
        source_types: Collection[str] | None = None, protected_record_ids: Collection[str] = (),
        protected_source_ids: Collection[str] = (), max_delete: int = 1000, dry_run: bool = False,
    ) -> dict[str, Any]:
        sources = DERIVED_PRUNABLE_SOURCE_TYPES if source_types is None else frozenset(source_types)
        if not sources <= DERIVED_PRUNABLE_SOURCE_TYPES:
            raise ValueError("derived pruning accepts only explicit derived source types")
        if not isinstance(max_delete, int) or isinstance(max_delete, bool) or max_delete < 0:
            raise ValueError("max_delete must be a nonnegative integer")
        ttl = float(ttl_seconds)
        cutoff = float(before) if before is not None else time.time() - ttl
        if not math.isfinite(ttl) or ttl < 0 or not math.isfinite(cutoff):
            raise ValueError("derived TTL and cutoff must be finite")
        plan = _DerivedPrunePlan(cutoff, sources, frozenset(protected_record_ids), frozenset(protected_source_ids), max_delete)
        return _prune_derived_records(self, plan, dry_run)


# LLM: 只携带本批不可变条件，不保存正文、连接或当前提交门的来源。
# 类用途: 打包TTL批次的白名单、引用保护和资源上限。
@dataclass(frozen=True)
class _DerivedPrunePlan:
    cutoff: float
    sources: frozenset[str]
    protected_records: frozenset[str]
    protected_sources: frozenset[str]
    limit: int


# LLM: 只活在一次持锁清理内；预算限制本批文件数和hash读取字节，不决定业务状态或丢弃欠账。
# 类用途: 合并本批路径引用、活跃组和剩余文件IO预算。
@dataclass
class _DerivedFileCleanupState:
    live_paths: set[Path]
    parents: dict[str, str]
    blocked: set[str]
    remaining_bytes: int


# LLM: 所有upsert与TTL共用同一个真实DB旁锁；C只把锁FD出生交原末门，flock等待仍在门外且零等待。
# 函数用途: 防止正文先写、SQLite尚未发表的记录被TTL当孤立文件删除。
def _derived_mutation_lock(store):
    options = {"blocking": False, "birth_commit": store._local_write} if store.coordinator_io else {}
    return locked_json_path(store.db_path.with_name(store.db_path.name + ".derived-records"), **options)


# LLM: 保持原body→records/FTS/event→commit算法和C固定事务，只额外与TTL清理互斥，不复制写入权威。
# 函数用途: 在正文发表全程保护同一命名空间，防止并发文件丢失。
def _upsert_record_serialized(store, params):
    with _derived_mutation_lock(store):
        if store.coordinator_io:
            return store._upsert_record_coordinator(params)
        prepared = store._prepare_record(params)
        with store._connection() as conn:
            created_at = store._existing_created_at(conn, prepared.record_id, prepared.now)
            store._upsert_record_row(conn, prepared, created_at)
            if store.fts_available:
                store._replace_fts_row(conn, prepared.record_id, prepared.title, prepared.content)
            store._record_upsert_event(conn, prepared)
            conn.commit()
        return store._prepared_record_result(prepared, created_at)


# LLM: 坏JSON、非对象和超递归数据均不具备清理资格，不把解析失败洗成空终态。
# 函数用途: 安全解析派生元数据，失败返回None以保留该记录。
def _derived_metadata(row):
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (ValueError, TypeError, RecursionError):
        return None
    return metadata if isinstance(metadata, dict) else None


# LLM: 运行关联只认source_id/run_id，不把root task的所有兄弟混成一条运行；task/request仅用于明确引用保护。
# 函数用途: 提取同一派生运行的稳定关联键。
def _derived_run_keys(row, metadata):
    values = [row["source_id"], (metadata or {}).get("run_id")]
    return {value for value in values if isinstance(value, str) and value}


# LLM: 显式记录和源引用永远优先；不从标题、正文或路径推断证据引用。
# 函数用途: 判断Compact、恢复或记忆候选是否保护这条派生记录。
def _derived_referenced(row, metadata, plan):
    values = [row["source_id"], *((metadata or {}).get(key) for key in ("run_id", "task_id", "request_id"))]
    return row["id"] in plan.protected_records or any(
        isinstance(value, str) and value in plan.protected_sources for value in values)


# LLM: 纯Gateway事件可无状态但必须有真实旧时间；其它缺状态快照不可假定结束，挂起标志始终阻止删除。
# 函数用途: 判断记录状态是否可信，不在这里应用年龄条件。
def _derived_state_safe(row, metadata):
    if metadata is None or _derived_notification_pending(metadata):
        return False
    if row["source_type"] == "gateway_event" and not metadata.get("status"):
        return True
    return derived_record_terminal(metadata)


# LLM: 路径压缩只合并同source/run关联；遍历用游标不水合正文，近期非终态也必须保护整组旧记录。
# 函数用途: 取得运行关联图中的最终组键。
def _derived_group_root(parents, key):
    parents.setdefault(key, key)
    while parents[key] != key:
        parents[key] = parents[parents[key]]
        key = parents[key]
    return key


# LLM: 始终扫描完整白名单而非仅本批source_types；另一类或30天内的活跃关联记录也能阻止本批删除。
# 函数用途: 用轻量关联键建立活跃/被引用运行保护伞，不读取正文。
def _derived_protected_groups(conn, plan):
    parents, unsafe = {}, set()
    sources = sorted(DERIVED_PRUNABLE_SOURCE_TYPES)
    sql = "SELECT id,source_id,source_type,metadata_json FROM records WHERE source_type IN (" + ",".join("?" for _ in sources) + ")"
    for row in conn.execute(sql, sources):
        metadata = _derived_metadata(row)
        keys = _derived_run_keys(row, metadata)
        if not keys:
            continue
        root = _derived_group_root(parents, next(iter(keys)))
        for key in keys:
            parents[_derived_group_root(parents, key)] = root
        if not _derived_state_safe(row, metadata) or _derived_referenced(row, metadata, plan):
            unsafe.update(keys)
    blocked = {_derived_group_root(parents, key) for key in unsafe}
    return parents, blocked


# LLM: 时间取行和载荷中全部有效时间的最新者；坏时间失败保留，cutoff边界严格小于。
# 函数用途: 检查数据库记录和事件本身均已老于截止时间。
def _derived_row_expired(row, metadata, cutoff):
    row_times = [_derived_timestamp(row[key]) for key in ("created_at", "updated_at")]
    if any(stamp is None or stamp >= cutoff for stamp in row_times):
        return False
    payload = dict(metadata)
    payload["status"] = "done"
    return derived_terminal_expired(payload, cutoff, row["updated_at"])


# LLM: 按(updated_at,id)递增，只有合格候选占max_delete配额；全部过滤用正式字段，不依赖正文。
# 函数用途: 选择有界的可删快照，支持dry-run与提交前重选。
def _derived_candidates(conn, plan):
    if not plan.sources or not plan.limit:
        return []
    parents, blocked = _derived_protected_groups(conn, plan)
    sources = sorted(plan.sources)
    sql = ("SELECT * FROM records WHERE source_type IN (" + ",".join("?" for _ in sources)
           + ") AND updated_at < ? ORDER BY updated_at ASC,id ASC")
    selected = []
    for row in conn.execute(sql, [*sources, plan.cutoff]):
        metadata = _derived_metadata(row)
        keys = _derived_run_keys(row, metadata)
        if any(_derived_group_root(parents, key) in blocked for key in keys):
            continue
        if not _derived_state_safe(row, metadata) or _derived_referenced(row, metadata, plan):
            continue
        if _derived_row_expired(row, metadata, plan.cutoff):
            selected.append(dict(row))
        if len(selected) >= plan.limit:
            break
    return selected


# LLM: C必须使用原固定事务入口；默认BEGIN IMMEDIATE同连接提交，异常由原连接出口回滚，不含文件IO。
# 函数用途: 执行TTL数据库短事务，不绕过现有提交门。
def _derived_transaction(store, operation):
    if store.coordinator_io:
        return store._coordinator_transaction(operation)
    with store._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = operation(conn)
        conn.commit()
        return result


# LLM: 待删文件与records/FTS删除同事务落账；坏账本不能静默覆盖导致失去中断恢复事实。
# 函数用途: 读取尚未完成的正文文件清理清单。
def _derived_pending_files(conn):
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (_DERIVED_PENDING_FILES_KEY,)).fetchone()
    entries = json.loads(row[0]) if row else []
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("invalid derived prune pending files")
    return entries


# LLM: 清单只含路径/hash/结构化引用，不复制正文；必须由调用方持事务并提交。
# 函数用途: 原子更新或清空待删文件账本。
def _derived_write_pending(conn, entries):
    if entries:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (_DERIVED_PENDING_FILES_KEY, json.dumps(entries, ensure_ascii=False, sort_keys=True)))
    else:
        conn.execute("DELETE FROM metadata WHERE key=?", (_DERIVED_PENDING_FILES_KEY,))


# LLM: FTS先删但以SAVEPOINT包住；触发器忽略records删除时必须恢复FTS，只有实际消失的行可进文件清单。
# 函数用途: 复核精确版本并在一笔事务删除记录和全文索引。
def _derived_delete_row(store, conn, row):
    current = conn.execute("SELECT * FROM records WHERE id=?", (row["id"],)).fetchone()
    if current is None or any(current[key] != row[key] for key in row):
        return False
    conn.execute("SAVEPOINT derived_prune_record")
    if store.fts_available:
        conn.execute("DELETE FROM records_fts WHERE id=?", (row["id"],))
    conn.execute("DELETE FROM records WHERE id=? AND updated_at=? AND content_hash=? AND content_path=?",
                 (row["id"], row["updated_at"], row["content_hash"], row["content_path"]))
    removed = conn.execute("SELECT 1 FROM records WHERE id=?", (row["id"],)).fetchone() is None
    if not removed:
        conn.execute("ROLLBACK TO derived_prune_record")
    conn.execute("RELEASE derived_prune_record")
    return removed


# LLM: 重新查询候选状态后仍只删原候选且版本未变；新活跃/引用关联出现时整组保护，不扩大本批配额。
# 函数用途: 在事务内重新核验并持久登记本批真正删除的文件。
def _derived_delete_batch(store, conn, plan, snapshots):
    current_ids = {row["id"] for row in _derived_candidates(conn, plan)}
    pending, deleted = _derived_pending_files(conn), []
    for row in snapshots:
        if row["id"] not in current_ids or not _derived_delete_row(store, conn, row):
            continue
        metadata = _derived_metadata(row) or {}
        pending.append({"id": row["id"], "content_path": row["content_path"], "content_hash": row["content_hash"],
                        "source_ids": [row["source_id"], *(metadata.get(key) for key in ("run_id", "task_id", "request_id"))]})
        deleted.append(row["id"])
    _derived_write_pending(conn, pending)
    return deleted


# LLM: 先验证解析后的边界，但保留原叶名由nofollow原语拒绝符号链接；不能把链接的目标当待删正文。
# 函数用途: 将旧正文路径约束到本Store的files目录内。
def _derived_file_parts(store, entry):
    path = store._resolve_content_path(entry["content_path"])
    root = store.files_dir.resolve()
    path.resolve().relative_to(root)
    return (path.parent.resolve() / path.name).relative_to(root).parts


# LLM: 正文只流式核对hash，不水合进内存；共享命名空间锁保证产品upsert不能在核对与unlink之间替换正文。
# 函数用途: 确认待删文件仍是已删记录的旧内容，变更后的文件保留。
def _derived_file_hash(root, parts, state):
    digest = hashlib.sha256()
    descriptor = open_readonly_file_beneath(root, parts)
    with os.fdopen(descriptor, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > state.remaining_bytes:
            raise ValueError("derived cleanup byte budget exceeded")
        read_bytes = 0
        while chunk := handle.read(min(65536, state.remaining_bytes)):
            state.remaining_bytes -= len(chunk)
            read_bytes += len(chunk)
            digest.update(chunk)
        if os.fstat(handle.fileno()).st_size != read_bytes:
            raise ValueError("derived cleanup file changed during bounded read")
    return digest.hexdigest()


# LLM: 引用比较规范化实际路径，兼容绝对/相对别名；解析失败不得推导为无引用。
# 函数用途: 在提交之后核实所有仍被数据库引用的正文路径。
def _derived_live_paths(store, conn):
    return {store._resolve_content_path(row[0]).resolve()
            for row in conn.execute("SELECT DISTINCT content_path FROM records")}


# LLM: 只有无引用且hash未变的受管普通文件才删；复用、内容变化、活跃组、坏条目、超预算和IO失败都保留恢复账。
# 函数用途: 安全处理一个已提交删除的正文，返回清理是否已经结算。
def _derived_cleanup_file(store, entry, state, plan):
    try:
        sources = entry.get("source_ids", [])
        keys = [value for value in sources if isinstance(value, str)]
        if entry["id"] in plan.protected_records or any(value in plan.protected_sources for value in keys):
            return False
        if any(_derived_group_root(state.parents, key) in state.blocked for key in keys):
            return False
        parts = _derived_file_parts(store, entry)
        root = store.files_dir.resolve()
        if root.joinpath(*parts).resolve() in state.live_paths:
            return False
        if _derived_file_hash(root, parts, state) != entry["content_hash"]:
            return False
        if store.coordinator_io:
            store._local_write(lambda: unlink_file_beneath(root, parts))
        else:
            unlink_file_beneath(root, parts)
        return True
    except FileNotFoundError:
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


# LLM: 文件IO在SQL提交后且共持upsert锁；每批最多1000文件/8MiB读取，未处理与失败项轮转保留，避免坏前缀饿死后续文件。
# 函数用途: 幂等清理待删正文并提交剩余欠账。
def _derived_cleanup_files(store, plan):
    with store._connection() as conn:
        pending = _derived_pending_files(conn)
        live_paths = _derived_live_paths(store, conn) if pending else set()
        parents, blocked = _derived_protected_groups(conn, plan) if pending else ({}, set())
    state = _DerivedFileCleanupState(live_paths, parents, blocked, _DERIVED_FILE_CLEANUP_MAX_BYTES)
    batch = pending[:_DERIVED_FILE_CLEANUP_MAX_FILES]
    remaining = [*pending[len(batch):], *(entry for entry in batch if not _derived_cleanup_file(store, entry, state, plan))]
    _derived_transaction(store, lambda conn: _derived_write_pending(conn, remaining))
    return len(remaining)


# LLM: dry-run不取出生锁、不改账、不删文件；真实批次只删除原候选快照且文件始终在SQL提交后处理。
# 函数用途: 编排有界TTL批次与中断恢复，返回真实已删ID和剩余文件欠账。
def _prune_derived_records(store, plan, dry_run):
    with store._connection() as conn:
        snapshots = _derived_candidates(conn, plan)
    result = {"cutoff": plan.cutoff, "dry_run": dry_run,
              "candidate_ids": [row["id"] for row in snapshots], "deleted_ids": [], "pending_files": 0}
    if dry_run:
        return result
    with _derived_mutation_lock(store):
        result["deleted_ids"] = _derived_transaction(store, lambda conn: _derived_delete_batch(store, conn, plan, snapshots))
        result["pending_files"] = _derived_cleanup_files(store, plan)
    return result
