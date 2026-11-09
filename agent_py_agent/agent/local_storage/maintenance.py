
from __future__ import annotations

# LLM: FTS只属可重建投影；持久影子任务分批推进，换表、完成键和事件同事务提交。
# LLM: C只消费当次scope；事件按seq前缀有界删除，封存段独立过期，旧逐行恢复账只做迁移清理。
# 模块用途: 提供索引维护、C完整重建和有界事件/封存段维护，不决定owner权限。

"""implements LocalStore maintenance, statistics, reset, FTS rebuild, and integrity checks.

这个文件只管维护类动作。
比如重建 FTS、清空索引准备重建、统计每种来源有多少记录，以及检查正文文件有没有丢。
"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import EllipsisType
from typing import TYPE_CHECKING, Any

from ..common.json_io import (
    _sync_parent_directory,
    locked_json_path,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
from ..io.sqlite_connection import sqlite_error_details
from ..runtime_db.maintenance import execute_runtime_statement
from .events import iter_event_jsonl_paths
from .search import FTS_ROWID_ALIGNED_KEY, fts_rowid_aligned

if TYPE_CHECKING:
    from ..user_space.storage_lanes import StorageLaneRequest

_EVENTS_PRUNE_PENDING_KEY = "events_prune_pending_ids"
_DEFAULT_WRITE_ATOMIC_UNLOCKED = write_private_text_file_atomic_unlocked
STEADY_STATE_EVENTS_KEEP_LAST = 20000
STEADY_STATE_EVENTS_MAX_DELETE = 5000

# 积压追平时单拍最多执行的事件裁剪步数。
LOCAL_STORE_CATCHUP_MAX_STEPS_COUNT = 16
# 单次增量回收最多释放的数据库页数。
LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT = 2048
# 空闲页占比达到该百分比才执行页回收。
LOCAL_STORE_VACUUM_FREE_PERCENT = 25
# 旧库一次性切换增量回收模式允许的最大库文件字节数。
LOCAL_STORE_VACUUM_CONVERT_MAX_BYTES = 512 * 1024 * 1024
# 执行整库重建前要求的最小剩余磁盘字节数。
LOCAL_STORE_VACUUM_MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024


# LLM: 维护器通过 schema 提供的连接访问当前库，不缓存其它用户数据；显式 reset 保持旧契约。
# LLM: C仅rebuild与prune_events固定叶闭合，其它维护writer仍各守原真实链；事件只删本批实际DB删除的ID。
# 类用途: 管理全文索引与有界事件维护，保同库事务、恢复账本和JSONL独有记录。
class LocalStoreMaintenanceMixin:

    # LLM: 回收独立于事件裁剪；C沿原事务/末门，NONE整库转换只准小库、足够磁盘且非C。
    # 函数用途: 有界释放增量库空闲页，或明确解释旧库为何需要离线维护。
    def incremental_vacuum_step(self, *, max_pages: int = LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT,
                                allow_convert: bool = False) -> dict:
        if type(max_pages) is not int or max_pages <= 0:
            raise ValueError("incremental vacuum page budget must be positive")
        connection = self._coordinator_read_connection if self.coordinator_io else self._connection
        try:
            with connection() as conn:
                before = _local_store_vacuum_state(conn)
            mode, free, pages, page_size = before
            before_size = os.stat(self.db_path).st_size
            due = _local_store_vacuum_due(before, max_pages)
            checkpoint_pending = _local_store_checkpoint_pending(self.db_path, before)
            mode_name = {0: "none", 1: "full", 2: "incremental"}.get(mode, "unknown")
            result = {"mode": mode_name, "performed": False}
            if mode not in (0, 2):
                return result
            if not due and not checkpoint_pending:
                return {**result, "deferred_reason": "below_threshold"}
            if mode == 0:
                reason = _local_store_conversion_deferral(
                    self.db_path, before, coordinator=self.coordinator_io, allow_convert=allow_convert)
                if reason is not None:
                    return {**result, "deferred_reason": reason}
                with self._connection() as conn:
                    conn.execute("PRAGMA auto_vacuum=INCREMENTAL").fetchall()
                    conn.execute("VACUUM").fetchall()
                    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?, 'false')",
                                 (FTS_ROWID_ALIGNED_KEY,))
                    conn.commit()
                mode_name = "converted"
            elif due:
                statement = f"PRAGMA incremental_vacuum({int(max_pages)})"
                if self.coordinator_io:
                    self._coordinator_transaction(lambda conn: execute_runtime_statement(conn, statement).fetchall())
                else:
                    with self._connection() as conn:
                        execute_runtime_statement(conn, statement).fetchall()
                        conn.commit()

            # LLM: checkpoint不在BEGIN内；C仍消费原末门，完整回填和物理缩小后才报释放量。
            # 函数用途: 返回SQLite自身的checkpoint状态，不把PASSIVE未回填完冒充成功。
            def checkpoint():
                with connection() as conn:
                    rows = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
                    busy, frames, copied = rows[0]
                    state = _local_store_vacuum_state(conn)
                    if not busy and frames == copied and _local_store_checkpoint_pending(self.db_path, state):
                        # 常驻连接仍可保留已回填WAL，零等待重置才能落实旧片已完成的逻辑截短。
                        timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
                        conn.execute("PRAGMA busy_timeout=0").fetchall()
                        try:
                            rows = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                        finally:
                            conn.execute(f"PRAGMA busy_timeout={timeout}").fetchall()
                    return rows

            if self.coordinator_io:
                checkpoint_rows = self._local_write(checkpoint)
            else:
                checkpoint_rows = checkpoint()
            with connection() as conn:
                _mode, after_free, after_pages, _page_size = _local_store_vacuum_state(conn)
            busy, frames, copied = checkpoint_rows[0]
            if busy or frames != copied or _local_store_checkpoint_pending(
                    self.db_path, (_mode, after_free, after_pages, page_size)):
                return {"mode": mode_name, "performed": False, "deferred_reason": "busy", "released_pages": 0,
                        "free_pages": after_free, "page_count": after_pages}
            physical_pages = max(0, before_size - os.stat(self.db_path).st_size) // page_size
            released_pages = (physical_pages if checkpoint_pending else
                              max(0, min(free - after_free, physical_pages)))
            return {"mode": mode_name, "performed": True,
                    "released_pages": released_pages,
                    "free_pages": after_free, "page_count": after_pages}
        except sqlite3.OperationalError as exc:
            if sqlite_error_details(exc).get("category") in {"busy", "locked"}:
                return {"performed": False, "deferred_reason": "busy"}
            raise

    # LLM: SQL每事务至多五千行；只按封存段尾事件检查过期，活动文件及其追加者互不干涉。
    # 函数用途: 后台推进一拍事件及段级维护，消化旧游标，不执行VACUUM。
    def prune_events_step(self, *, before: float, keep_last: int = 1000, max_delete: int = 256,
                          max_bytes: int = 256 * 1024):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("event scan byte budget must be positive")
        options = (float(before), max(0, int(keep_last)), min(STEADY_STATE_EVENTS_MAX_DELETE, max(1, int(max_delete))))
        # 同类step串行，但不反向持JSONL锁等待SQLite；旧append/显式维护仍用原锁。
        step_lock = (self._coordinator_events_lock() if self.coordinator_io else
                     locked_json_path(self.events_path.with_name(self.events_path.name + ".prune")))
        with step_lock:
            selected, deleted, targets = _event_step_transaction(self, lambda conn: _event_step_prepare(conn, options))
            progress = _event_jsonl_step(self, targets, max_bytes)
        with self._connection() as conn:
            free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        return {**_event_prune_result(selected, deleted, progress["removed_segments"]),
                "partial": not progress["complete"], "cursor": progress["cursor"],
                "scanned_bytes": progress["scanned_bytes"], "expired_rows": {"deleted": len(deleted)},
                "removed_segments": progress["removed_segments"], "removed_bytes": progress["removed_bytes"],
                "errors": progress["errors"],
                "free_page_reuse": {"available_pages": free, "page_count": pages},
                "physical_shrink": {"performed": False, "reason_code": "explicit_maintenance_required"}}

    # LLM: 只选seq连续前缀，SQL提交后独立过期封存段；预演不触碰文件或旧恢复账。
    # 函数用途: 裁剪一批旧事件并消化旧pending，不再逐行重写JSONL。
    def prune_events(self, *, before: float, keep_last: int = 1000, max_delete: int = 5000,
                     dry_run: bool = False) -> dict[str, int | bool]:
        options = (float(before), max(0, int(keep_last)), min(STEADY_STATE_EVENTS_MAX_DELETE, max(1, int(max_delete))))
        with self._connection() as conn:
            _read_pending_event_ids(conn)
            selected = _select_prunable_event_ids(conn, *options)
        if dry_run:
            return _event_prune_result(len(selected), set(), 0, dry_run=bool(dry_run))
        step_lock = (self._coordinator_events_lock() if self.coordinator_io else
                     locked_json_path(self.events_path.with_name(self.events_path.name + ".prune")))
        with step_lock:
            selected_count, deleted, targets = _event_step_transaction(
                self, lambda conn: _prepare_event_prune(conn, options))
            # SQL 已提交；封存段 unlink 不持 SQLite 写锁，也不改活动文件。
            removed = self._prune_event_jsonl(set(targets))
        return _event_prune_result(selected_count, deleted, removed)

    # LLM: 只由显式/后台维护周期调用，每次至多max_delete；不在单事件热路径扫描，不VACUUM或清理records正文。
    # 函数用途: 按默认保留两万条、每批最多五千条执行一次可恢复维护。
    def prune_events_steady_state(self, *, keep_last: int = STEADY_STATE_EVENTS_KEEP_LAST,
                                 max_delete: int = STEADY_STATE_EVENTS_MAX_DELETE,
                                 before: float | None = None, dry_run: bool = False) -> dict[str, int | bool]:
        return self.prune_events(before=time.time() if before is None else before,
                                 keep_last=keep_last, max_delete=max_delete, dry_run=dry_run)

    # LLM: SQL已提交；段尾只读在门外，封存段unlink沿C原_local_write，活动文件永不改写。
    # 函数用途: 保留原整数接口，返回本拍整段删除数。
    def _prune_event_jsonl(self, target_ids: set[str]) -> int:
        from ..user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES
        return _event_jsonl_step(self, target_ids, STORAGE_LANE_SLICE_BYTES)["removed_segments"]

    # LLM: 原钩子只在真正准备时消耗；大单文档仅一次有上限单行重试，不跳过正文或CAS。
    # 函数用途: 保留原无参整数接口并恢复或开始一次完整重建。
    def rebuild_fts(self) -> int:
        if not self.fts_available:
            return 0
        if _FTS_BATCH_LIMITS.get() is not None:
            # 有界调用中的 schema 初始化不得暗中同步跑完整个重建；原 pending 留给分步口。
            raise _FtsRebuildDeferred('fts rebuild deferred to bounded step')
        start_new = True
        original_prepare = self._prepare_fts_rebuild
        prepare_called = False

        # LLM: 栈内标记仅在实际调用旧钩子时改变，retiring前置返回不消耗它。
        # 函数用途: 第一次走旧准备钩子，后续每批仍真实准备。
        def prepare():
            nonlocal prepare_called
            if not prepare_called:
                prepare_called = True
                return original_prepare()
            return _fts_prepare(self)

        while True:
            limits = _fts_limits(256, 8 * 1024 * 1024, 250, None)
            try:
                result = self._advance_fts_rebuild(limits, None, start_new, prepare)
            except _FtsByteBudget as exc:
                if not limits[1] < exc.required_bytes <= _FTS_SYNC_SINGLE_MAX_BYTES:
                    raise
                single = _fts_limits(1, exc.required_bytes, 250, None)
                result = self._advance_fts_rebuild(single, None, start_new, prepare)
            if result['status'] == 'complete':
                return int(result['copied'])
            if result['status'] != 'retiring':
                start_new = False
            if not result.get('progressed', False):
                raise _FtsRebuildDeferred('fts rebuild deferred: batch budget exhausted')

    # LLM: 正文准备和维护者锁等待在门外；C的新写仅沿原事务端口，旧active始终保留到发表。
    # 函数用途: 推进一个有行数、字节及协作时间预算的可恢复批次。
    def rebuild_fts_step(self, *, max_rows=256, max_bytes=8 * 1024 * 1024,
                         max_ms=250, max_records=None, cancellation=None, start_new=False):
        if not self.fts_available:
            return {'status': 'complete', 'copied': 0, 'job_id': None, 'progressed': False}
        limits = _fts_limits(max_rows, max_bytes, max_ms, max_records)
        return self._advance_fts_rebuild(limits, cancellation, start_new, self._prepare_fts_rebuild)

    # LLM: 同步与显式step共用唯一状态机；准备依赖每批执行，验证及提交从不绕过原真实叶。
    # 函数用途: 在维护旁锁内推进一拍，兼容旧同步一次准备入口与新分批入口。
    def _advance_fts_rebuild(self, limits, cancellation, start_new, prepare):
        _fts_check_cancel(cancellation)
        with _fts_rebuild_lock(self):
            with self._connection() as conn:
                job = _fts_job(conn)
            if job is None or (job['phase'] == 'complete' and start_new):
                _fts_check_space(self)
                if job is not None:
                    cleanup = _fts_cleanup_locked(self, limits)
                    if cleanup['pending']:
                        return _fts_reply(job, 'retiring', cleanup['removed'] > 0)
                _fts_start(self, job)
            elif job['phase'] == 'complete':
                self._fts_needs_rebuild = False
                return _fts_reply(job, 'complete', False)
            _fts_check_cancel(cancellation)
            token = _FTS_BATCH_LIMITS.set(limits)
            try:
                snapshot = prepare()
            finally:
                _FTS_BATCH_LIMITS.reset(token)
            _fts_check_cancel(cancellation)
            if (not snapshot.finished and not snapshot.selected
                    and snapshot.mode in {'copy', 'delta', 'verify', 'map_gc', 'map'}):
                return _fts_reply(snapshot.job, 'partial', False)

            # LLM: 原验证叶在任何影子写前执行；回调及完整正文仅存当前栈。
            # 函数用途: 验证并提交本批或最终换名。
            def operation(conn):
                self._validate_fts_rebuild(conn, snapshot)
                return _fts_apply(self, conn, snapshot)

            publishing = snapshot.mode == 'map' and snapshot.finished
            if publishing and self.coordinator_io:
                with self._coordinator_events_lock():
                    result = _fts_transaction(self, operation)
            else:
                result = _fts_transaction(self, operation)
            self._fts_needs_rebuild = result['status'] != 'complete'
            return result

    # LLM: 退休副本不在发表事务内DROP，清理仍复用原写门及删除防护。
    # 函数用途: 显式分批回收旧索引和manifest。
    def cleanup_fts_retired_step(self, *, max_rows=256, max_bytes=8 * 1024 * 1024,
                                 max_ms=250, cancellation=None):
        limits = _fts_limits(max_rows, max_bytes, max_ms, None)
        _fts_check_cancel(cancellation)
        with _fts_rebuild_lock(self):
            return _fts_cleanup_locked(self, limits)

    # LLM: 兼容入口共用持久状态机，不保留另一套清空active的重建实现。
    # 函数用途: 转发旧coordinator重建调用。
    def _rebuild_fts_coordinator(self):
        return self.rebuild_fts()

    # LLM: 原无参同步入口一次调用合同保留；显式step每次调用，ContextVar只含纯预算。
    # 函数用途: 在写门外准备本批完整raw行及正文。
    def _prepare_fts_rebuild(self):
        return _fts_prepare(self)

    # LLM: 比较本批完整raw值及类型、任务代次、结构及文件版本；不在写门重读正文。
    # 函数用途: 保留原验证注入点，在影子写和发表前拒绝变化。
    def _validate_fts_rebuild(self, conn, snapshot):
        _fts_validate(self, conn, snapshot)

    def reset(self, *, remove_content_files: bool = False, reset_events_file: bool = True) -> None:
        with self._connection() as conn:
            if self.fts_available:
                conn.execute("DELETE FROM records_fts")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM records")
            conn.commit()
        if reset_events_file:
            ensure_private_dir(self.events_path.parent)
            self.events_path.write_text("", encoding="utf-8")
        if remove_content_files:
            self._remove_content_files()

    def _remove_content_files(self) -> None:
        """Best-effort cleanup for content files after database reset."""
        if not self.files_dir.exists():
            return
        for path in self.files_dir.glob("*.txt"):
            try:
                path.unlink()
            except OSError:
                continue

    def count_records(self, *, source_type: str | None = None) -> int:
        """统计记录数，可按 source_type 过滤。"""

        where, params = self._record_filters(source_type=source_type)
        with self._connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM records {where}", params).fetchone()[0])

    def source_counts(self) -> dict[str, int]:
        """按 source_type 汇总记录数。"""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT source_type, COUNT(*) AS count FROM records GROUP BY source_type ORDER BY source_type"
            ).fetchall()
        return {str(row["source_type"]): int(row["count"]) for row in rows}

    def missing_content_files(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """检查 records 指向的正文文件是否还存在。"""

        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_id, title, content_path
                FROM records
                ORDER BY updated_at DESC
                """
            ).fetchall()
        missing: list[dict[str, Any]] = []
        for row in rows:
            path = self._resolve_content_path(row["content_path"])
            if path.exists():
                continue
            missing.append(
                {
                    "id": row["id"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "title": row["title"],
                    "content_path": str(path),
                }
            )
            if limit > 0 and len(missing) >= limit:
                break
        return missing

    def stats(self) -> dict[str, Any]:
        """返回本地事实源的当前状态。"""

        with self._connection() as conn:
            record_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "files_dir": str(self.files_dir),
            "events_path": str(self.events_path),
            "fts5_enabled": self.fts_available,
            "record_count": record_count,
            "event_count": event_count,
        }


# LLM: 固定只读PRAGMA，不创建连接、不提交；调用者决定使用普通或C读取入口。
# 函数用途: 同一连接读取回收模式、空闲页、总页数和页字节数。
def _local_store_vacuum_state(conn):
    return tuple(int(conn.execute(f"PRAGMA {name}").fetchone()[0])
                 for name in ("auto_vacuum", "freelist_count", "page_count", "page_size"))


# LLM: FULL库由SQLite自己管理；NONE只按比例转换，INCREMENTAL也允许达到绝对页预算时推进。
# 函数用途: 用纯状态判断有没有达到页回收阈值。
def _local_store_vacuum_due(state, max_pages=LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT):
    mode, free, pages, _page_size = state
    ratio_due = free * 100 >= LOCAL_STORE_VACUUM_FREE_PERCENT * max(1, pages)
    return (mode == 0 and ratio_due) or (mode == 2 and (free >= max_pages or ratio_due))


# LLM: 只stat主库，不开关原db/sidecar文件FD；读者阻碍checkpoint时，逻辑已缩小但物理仍大须继续延期。
# 函数用途: 识别增量库尚未完成物理截短的回填责任。
def _local_store_checkpoint_pending(db_path, state):
    mode, _free, pages, page_size = state
    return mode == 2 and os.stat(db_path).st_size > pages * page_size


# LLM: WAL可能承载尚未回填的增长，转换资源限额必须取逻辑库和主文件中的较大值。
# 函数用途: 只读判定旧NONE库是否允许整库转换，C及资源不足不构造新的维护写者。
def _local_store_conversion_deferral(db_path, state, *, coordinator, allow_convert):
    if coordinator:
        return "coordinator_conversion_refused"
    size = max(os.stat(db_path).st_size, state[2] * state[3])
    if not allow_convert or size > LOCAL_STORE_VACUUM_CONVERT_MAX_BYTES:
        return "offline_vacuum_required"
    if shutil.disk_usage(db_path.parent).free < LOCAL_STORE_VACUUM_MIN_FREE_DISK_BYTES + 2 * size:
        return "insufficient_disk"
    return None


# LLM: metadata旧pending也属于恢复责任，不能只看已经删空的SQL范围而漏掉JSONL清账。
# 函数用途: 在一个只读事务快照中取得事件积压、恢复标记和页回收状态。
def _local_events_snapshot(conn, db_path):
    # 两个独立极值子查询才命中SQLite MIN/MAX优化；合并聚合会全扫历史事件索引。
    lo, hi = conn.execute("SELECT (SELECT MIN(seq) FROM events), (SELECT MAX(seq) FROM events)").fetchone()
    backlog = max(0, hi - lo + 1 - STEADY_STATE_EVENTS_KEEP_LAST) if lo is not None and hi is not None else 0
    if backlog and conn.execute("SELECT 1 FROM events ORDER BY seq LIMIT 1 OFFSET ?",
                                (STEADY_STATE_EVENTS_KEEP_LAST,)).fetchone() is None:
        # seq有空洞时跨度会高估；至多检查固定保留窗口，不能让已经空闲的库永久假积压。
        backlog = 0
    resumable = conn.execute(
        "SELECT 1 FROM metadata WHERE key IN ('events_prune_cursor', 'events_prune_pending_ids') LIMIT 1"
    ).fetchone() is not None
    state = _local_store_vacuum_state(conn)
    return backlog, resumable, state, _local_store_checkpoint_pending(db_path, state)


# LLM: 仅SQLite API打开发现器给出的既有库，mode=ro不改业务/schema；允许SQLite管理自身WAL/SHM。
#   此辅助文件边界由恢复派发显式批准；不用immutable忽略活跃WAL，也不手工开关或删除db/sidecar FD。
# 函数用途: 构造Store前识别真正空闲的库，保证协调器空闲片零业务提交。
def _local_events_precheck(db_path):
    try:
        if os.stat(db_path).st_size == 0:
            return None
    except FileNotFoundError:
        return None
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.0)
    try:
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('events','metadata')")}
        return _local_events_snapshot(conn, db_path) if tables == {"events", "metadata"} else None
    finally:
        conn.close()


def _local_events_alignment_needed(conn):
    """只读预检复用 R2 的证明；空索引缺标记时不在只读连接中盖章。"""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='records_fts'").fetchone() is None:
        return False
    metadata = dict(conn.execute("SELECT key,value FROM metadata WHERE key IN (?, 'records_fts_rebuild_pending')",
                                 (FTS_ROWID_ALIGNED_KEY,)).fetchall())
    if (FTS_ROWID_ALIGNED_KEY not in metadata and metadata.get('records_fts_rebuild_pending') != 'true'
            and conn.execute('SELECT 1 FROM records_fts LIMIT 1').fetchone() is None):
        return False
    return not fts_rowid_aligned(conn)


def _local_events_extra_work(db_path):
    """预检只发现封存段责任，段尾读取留给执行步骤以免重复消耗预算。"""
    conn = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=0.0)
    try:
        active = db_path.parent / 'events.jsonl'
        return any(path != active for path in iter_event_jsonl_paths(active))
    finally:
        conn.close()


# LLM: 只驱动原冻结裁剪step；最多16拍，后续拍受累计JSONL字节和协作时间限制，首拍即使过期也推进。
# 函数用途: 追赶一个Store并在事件阶段后执行一次独立页回收。
def _local_events_advance(store, request, snapshot, budget, receipt, *, segments_due=False):
    if snapshot is None:
        with store._connection() as conn:
            snapshot = _local_events_snapshot(conn, store.db_path)
    backlog, resumable = snapshot[:2]
    deleted_db = deleted_jsonl = 0
    scanned_bytes = receipt['bytes']
    segments_pending = segments_due
    if backlog or resumable or segments_due:
        for step in range(LOCAL_STORE_CATCHUP_MAX_STEPS_COUNT):
            if step and (budget.remaining_ms() <= 0 or budget.exhausted(size=scanned_bytes)):
                break
            remaining = budget.max_bytes - scanned_bytes
            if remaining <= 0:
                break
            result = store.prune_events_step(
                before=request.now, keep_last=STEADY_STATE_EVENTS_KEEP_LAST,
                max_delete=STEADY_STATE_EVENTS_MAX_DELETE, max_bytes=remaining)
            deleted_db += result["deleted_db"]
            deleted_jsonl += result["deleted_jsonl"]
            scanned_bytes += result["scanned_bytes"]
            segments_pending = result['partial']
            backlog = max(0, backlog - result["deleted_db"])
            receipt.update(deleted_items=deleted_db, bytes=scanned_bytes, backlog=backlog)
            if result.get('errors'):
                from ..user_space.storage_lanes import STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT
                receipt['errors'] = (receipt['errors'] + result['errors'])[:STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT]
                break
            if not result["partial"] and result["deleted_db"] == 0:
                break
    if budget.exhausted(size=scanned_bytes):
        vacuum = {"performed": False, "deferred_reason": "budget_exhausted"}
    else:
        vacuum = store.incremental_vacuum_step(allow_convert=not request.coordinator)
    with store._connection() as conn:
        after = _local_events_snapshot(conn, store.db_path)
    return deleted_db, deleted_jsonl, scanned_bytes, vacuum, after, segments_pending


# LLM: owner发现器是唯一路径来源；空闲预检不构造Store，实际维护只沿LocalStore原_local_write写门。
#   游标额外保存一个布尔cycle_pending，跨DB证明本轮全部空闲；不另建维护状态或配额账。
# 函数用途: 每片轮转一个库，追平事件与JSONL后回收页，返回严格领域回执。
def maintain_local_events_lane(request: StorageLaneRequest) -> dict:
    from ..common.global_write_commit import global_write_commit_scope
    from ..local_storage import LocalStore
    from ..user_space.storage_lanes import (
        STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT,
        STORAGE_LANE_SLICE_BYTES,
        STORAGE_LANE_SLICE_ENTRIES,
        STORAGE_LANE_SLICE_MS,
        lane_cursor_window,
        owner_local_store_databases,
    )

    receipt = {"cursor": {}, "complete": True, "items": 0, "bytes": 0, "deleted_items": 0,
               "freed_bytes": 0, "backlog": 0, "skipped": {"vacuum_deferred": 0}, "errors": []}
    if request.legal_hold:
        return {**receipt, "deferred_reason": "legal_hold"}
    paths, discovery_errors = owner_local_store_databases(request.home)
    for error in discovery_errors[:STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT]:
        item = {"code": "local_store_discovery"}
        source = error.get("source")
        if isinstance(source, str) and not Path(source).is_absolute() and ".." not in Path(source).parts:
            item["path"] = source
        receipt["errors"].append(item)
    receipt["complete"] = not discovery_errors
    by_name = {path.relative_to(request.home.owner_home_dir).as_posix(): path for path in paths}
    names = sorted(by_name)
    if not names:
        return receipt
    cursor = request.cursor
    selected = lane_cursor_window(cursor, names, 1)
    if not selected:
        cursor = {}
        selected = lane_cursor_window(cursor, names, 1)
    name = selected[0]
    cycle_pending = bool(cursor.get("cycle_pending", False))
    receipt.update(cursor={"after": name, "phase": "events", "cycle_pending": cycle_pending},
                   complete=False, items=1)
    db_path = by_name[name]
    source_failure = []

    # LLM: 仍调用原request.commit，不制造授权；区分原门自身异常与operation原异常，拒绝后不再消费来源。
    # 函数用途: 让上层保留原提交门错误的同一对象，不将撤权或未知执行改成普通维护失败。
    def commit(operation):
        if source_failure:
            raise source_failure[0]
        operation_failure = []

        # LLM: 只运输原操作并记录失败对象，不新增业务写入或重试。
        # 函数用途: 辨认异常是否确实来自本次数据库/文件操作内部。
        def run():
            try:
                return operation()
            except BaseException as exc:
                operation_failure.append(exc)
                raise

        try:
            return request.commit(run)
        except BaseException as exc:
            if not operation_failure or exc is not operation_failure[0]:
                source_failure.append(exc)
            raise

    try:
        snapshot = _local_events_precheck(db_path)
        event_work = snapshot is not None and bool(snapshot[0] or snapshot[1])
        segment_work = False
        if snapshot is not None:
            segment_work = _local_events_extra_work(db_path)
        vacuum = {"performed": False, "deferred_reason": "below_threshold"}
        vacuum_work = snapshot is not None and bool(snapshot[3] or _local_store_vacuum_due(snapshot[2]))
        if vacuum_work and snapshot[2][0] == 0:
            reason = _local_store_conversion_deferral(
                db_path, snapshot[2], coordinator=request.coordinator, allow_convert=not request.coordinator)
            if reason is not None:
                vacuum_work = False
                vacuum["deferred_reason"] = reason
        initial_work = snapshot is None or event_work or vacuum_work or segment_work
        if initial_work:
            budget = request.budget.capped(items=STORAGE_LANE_SLICE_ENTRIES,
                                          size=STORAGE_LANE_SLICE_BYTES, ms=STORAGE_LANE_SLICE_MS)
            if (request.budget.max_bytes == 0 or request.budget.max_items == 0
                    or (request.budget.remaining_ms() <= 0 and not event_work)):
                receipt["backlog"] = snapshot[0] if snapshot else 0
                return {**receipt, "cursor": dict(cursor), "deferred_reason": "budget_exhausted"}
            scope = global_write_commit_scope(commit) if request.coordinator else nullcontext()
            with scope:
                # schema 恢复 pending 时只保留原责任，不在有界通道构造中同步跑完整库。
                token = _FTS_BATCH_LIMITS.set((budget.max_items, budget.max_bytes, budget.deadline))
                try:
                    store = LocalStore(db_path, **({"coordinator_io": True} if request.coordinator else {}))
                finally:
                    _FTS_BATCH_LIMITS.reset(token)
                deleted, mirrored, scanned, vacuum, after, segments_pending = _local_events_advance(
                    store, request, snapshot, budget, receipt, segments_due=segment_work)
                if source_failure:
                    raise source_failure[0]
            receipt.update(deleted_items=deleted, bytes=scanned,
                           freed_bytes=vacuum.get("released_pages", 0) * after[2][3])
            cycle_pending = cycle_pending or event_work or vacuum_work or bool(
                deleted or mirrored or segments_pending or vacuum.get("performed"))
            snapshot = after
        receipt["backlog"] = snapshot[0]
        pending = bool(snapshot[0] or snapshot[1] or snapshot[3] or _local_store_vacuum_due(snapshot[2]))
        cycle_pending = cycle_pending or pending
        if "deferred_reason" in vacuum:
            receipt["skipped"]["vacuum_deferred"] = 1
            receipt["deferred_reason"] = vacuum["deferred_reason"]
        receipt["cursor"].update(phase="events" if pending else "vacuum", cycle_pending=cycle_pending)
        receipt["complete"] = name == names[-1] and not cycle_pending and not discovery_errors
    except (sqlite3.Error, OSError) as exc:
        if source_failure and exc is source_failure[0]:
            raise
        if isinstance(exc, sqlite3.OperationalError) and sqlite_error_details(exc).get("category") not in {"busy", "locked"}:
            raise
        reason = "busy" if sqlite_error_details(exc).get("category") in {"busy", "locked"} else "local_store_maintenance"
        if len(receipt["errors"]) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
            receipt["errors"].append({"code": reason, "path": name})
        receipt["deferred_reason"] = reason
        receipt["cursor"]["cycle_pending"] = True
    return receipt


# LLM: pending只接受非空字符串ID数组；坏账拒绝继续删除，避免覆盖不可解释的恢复状态。
# 函数用途: 从同库metadata读取上次尚未从JSONL移除的精确事件ID。
def _read_pending_event_ids(conn) -> list[str]:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (_EVENTS_PRUNE_PENDING_KEY,)).fetchone()
    if row is None:
        return []
    pending = json.loads(row[0])
    if not isinstance(pending, list) or any(not isinstance(item, str) or not item for item in pending):
        raise ValueError("invalid_events_prune_pending_ids")
    return list(dict.fromkeys(pending))


# LLM: 只在已有写事务内更新固定metadata键；空账删除键，不碰schema/FTS及其它维护标记。
# 函数用途: 将仍待擦除的事件ID数组保存到原库的恢复账本。
def _write_pending_event_ids(conn, pending):
    if pending:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (_EVENTS_PRUNE_PENDING_KEY, json.dumps(pending)))
    else:
        conn.execute("DELETE FROM metadata WHERE key=?", (_EVENTS_PRUNE_PENDING_KEY,))


# LLM: max_delete已由公开入口归一为正数；小额旧账仍可合并，达到本批或5000阈值则先恢复不扩账。
# 函数用途: 用同一只读判据限制预查与事务内的新删除批次准入。
def _event_prune_pending_is_full(pending, max_delete):
    return len(pending) >= min(max_delete, STEADY_STATE_EVENTS_MAX_DELETE)


# LLM: 旧pending仅供消化；本拍按seq前缀删除，触发器保留的行不计入已删集。
# 函数用途: 准备有界SQL删除批次，不再新增逐行重写责任。
def _prepare_event_prune(conn, options):
    pending = _read_pending_event_ids(conn)
    selected, deleted = _delete_prunable_events(conn, *options)
    targets = list(dict.fromkeys([*pending, *sorted(deleted)]))
    # Cat3-R4：旧 pending 只迁移消化，新的 SQL 删除不再登记逐行重写责任。
    return selected, deleted, targets


# LLM: JSONL已成功处理targets；重新读取当前账，只减本批ID以保留文件锁释放后并发登记的新批次。
# 函数用途: 清除已经完成的恢复责任，不误删其它维护者的新挂起ID。
def _clear_pending_event_ids(conn, targets):
    completed = set(targets)
    remaining = [item for item in _read_pending_event_ids(conn) if item not in completed]
    _write_pending_event_ids(conn, remaining)


# LLM: 普通模式必须在释放events文件锁之后才能重取SQLite写锁；BEGIN下读改写账本防并发丢更新。
# 函数用途: 用独立短事务完成普通模式的恢复清账，失败保留可幂等重试的pending。
def _clear_pending_events_transaction(store, targets):
    with store._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _clear_pending_event_ids(conn, targets)
        conn.commit()


# LLM: 原输入按二进制LF边界读取，只有strict UTF-8及JSON均有效的命中行可删除；内存只随单记录变化。
# 函数用途: 将未命中的行逐字节流式复制到同目录暂存文件，不拼接整份流水。
def _copy_retained_event_lines(path, retained, target_ids):
    removed = 0
    with path.open("rb") as handle:
        for line in handle:
            if _pruned_event_line(line, target_ids):
                removed += 1
            else:
                retained.write(line)
    return removed


# LLM: 只替换同events_path且调用者持原锁；生产路径不读回暂存正文，C仅一次replace沿原末门。
# 函数用途: 原子发表已同步的流式文件；保留既有替换函数的monkeypatch故障注入合同。
def _publish_pruned_event_file(store, temporary):
    if write_private_text_file_atomic_unlocked is not _DEFAULT_WRITE_ATOMIC_UNLOCKED:
        # 仅过滤完成后的测试替身兼容发表允许surrogateescape，不能用它判断输入行是否合法。
        content = temporary.read_bytes().decode("utf-8", errors="surrogateescape")
        operation = partial(write_private_text_file_atomic_unlocked,
            store.events_path, content, preserve_bytes=True,
            **({"replace_once": True} if store.coordinator_io else {}))
    else:
        operation = partial(os.replace, temporary, store.events_path)
    if store.coordinator_io:
        store._local_write(operation)
    else:
        operation()


# LLM: 同目录mkstemp独占0600且只写原始bytes；fsync后替换，无命中仍补父目录同步以覆盖上次partial窗口。
# 函数用途: 以不随文件行数增长的内存执行重写，无命中时不替换原文件。
def _stream_prune_event_jsonl(store, target_ids):
    descriptor, name = tempfile.mkstemp(prefix=f".{store.events_path.name}.", suffix=".tmp",
                                        dir=store.events_path.parent)
    temporary = Path(name)
    primary = None
    try:
        with os.fdopen(descriptor, "wb") as retained:
            removed = _copy_retained_event_lines(store.events_path, retained, target_ids)
            if removed:
                retained.flush()
                os.fsync(retained.fileno())
        if removed:
            _publish_pruned_event_file(store, temporary)
        _sync_parent_directory(store.events_path)
        return removed
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _discard_event_temp(temporary, primary)


# LLM: 清理失败不盖掉原始读取/同步/撤权异常；无主错误时清理失败照常传播，pending仍不清。
# 函数用途: 尽力删除本次暂存文件，并保留真正导致裁剪失败的异常身份。
def _discard_event_temp(temporary, primary):
    try:
        temporary.unlink(missing_ok=True)
    except BaseException:
        if primary is None:
            raise


# LLM: 只读当前事务中的events；时间相等时沿原timeline的seq定序，不以JSONL、显示时间或条数猜删除集合。
# 函数用途: 选出不在最新保护窗口内、严格过期且至多max_delete条的事件ID。
def _select_prunable_event_ids(conn, before: float, keep_last: int, max_delete: int) -> set[str]:
    # 保护窗口沿 seq，与 JSONL 追加次序一致；时间相等不再改变窗口或删除次序。
    boundary = conn.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?", (keep_last,)).fetchone()
    if boundary is None:
        return set()
    selected = set()
    for event_id, created_at in conn.execute(
            "SELECT event_id, created_at FROM events WHERE seq <= ? ORDER BY seq ASC LIMIT ?",
            (boundary[0], max_delete)):
        if created_at >= before:
            break
        selected.add(event_id)
    return selected


# LLM: 同一维护事务端口，不另开写者；SQL提交后才进行独立的段级文件操作。
# 函数用途: 提交一个可恢复事件维护步骤。
def _event_step_transaction(store, operation):
    if store.coordinator_io:
        return store._coordinator_transaction(operation)
    with store._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = operation(conn)
        conn.commit()
        return result


# 续扫前核对的前缀样本窗口：文件头与已提交offset之前各取至多这么多字节，正常追加不改变这两段。
_EVENT_STEP_PROBE_BYTES = 4096
_EVENT_STEP_TEMP_NAME = re.compile(r"\.events-prune-[a-zA-Z0-9_-]+\.step")


# LLM: 存量游标不再恢复逐行重写，SQL始终选择当次事务中的有界seq前缀。
# 函数用途: 沿同一事务准备本拍删除，旧恢复账交给段级步骤消化。
def _event_step_prepare(conn, options):
    return _prepare_event_prune(conn, options)


# LLM: 坏JSON或非对象按不可续扫（空dict）处理，由调用方从0重扫且保留pending；缺行返回None。
# 函数用途: 读取同库metadata中的流式维护游标。
def _read_event_step_job(conn):
    row = conn.execute("SELECT value FROM metadata WHERE key='events_prune_cursor'").fetchone()
    if row is None:
        return None
    try:
        job = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return job if isinstance(job, dict) else {}


# LLM: 只认本版完整字段；旧版含size/mtime的revision游标、坏偏移、坏尾字节或坏暂存名都不可续扫，只能从0重扫。
# 函数用途: 校验游标结构，避免不可信偏移或暂存名进入续扫。
def _event_step_job_valid(job):
    source, targets, tail, name = job.get("source"), job.get("targets"), job.get("tail_hex"), job.get("temporary")
    return (isinstance(source, list) and len(source) == 2 and all(type(item) is int for item in source)
            and isinstance(targets, list) and bool(targets)
            and all(isinstance(item, str) and item for item in targets)
            and all(type(job.get(key)) is int and job[key] >= 0 for key in ("offset", "output_offset", "removed"))
            and isinstance(tail, str) and re.fullmatch(r"(?:[0-9a-f]{2})*", tail) is not None
            and isinstance(job.get("prefix"), str)
            and isinstance(name, str) and _EVENT_STEP_TEMP_NAME.fullmatch(name) is not None)


# LLM: 身份只取dev/ino，正常追加只改变字节数而不再使游标失效；换代、截断与前缀改写由调用方另行判定。
# 函数用途: 返回JSONL来源身份[dev, ino]与当前字节数，接受路径或已打开FD。
def _event_file_revision(path):
    info = os.stat(path)
    return [info.st_dev, info.st_ino], info.st_size


# LLM: 样本走同一已打开FD，只读文件头与已提交offset前各至多4KiB，不随文件变大，也不计入扫描预算。
# 函数用途: 读取前缀完整性样本：文件头窗口与游标之前的边界窗口。
def _event_prefix_windows(source, offset):
    source.seek(0)
    head = source.read(min(_EVENT_STEP_PROBE_BYTES, offset))
    start = max(0, offset - _EVENT_STEP_PROBE_BYTES)
    source.seek(start)
    return head, source.read(offset - start)


# LLM: 游标只存摘要，不在metadata复制事件正文；头长度进入摘要，避免两段拼接产生歧义。
# 函数用途: 把两个样本窗口压成游标保存的前缀摘要。
def _event_prefix_digest(head, boundary):
    return hashlib.sha256(len(head).to_bytes(8, "big") + head + boundary).hexdigest()


# LLM: 临时文件只准原events目录中mkstemp的准确basename，nofollow单链接且0600；坏游标不能指向任意路径。
# 函数用途: 校验并打开原流式维护的暂存文件。
def _event_step_temp(store, job):
    name = job.get("temporary", "")
    if not isinstance(name, str) or _EVENT_STEP_TEMP_NAME.fullmatch(name) is None:
        raise ValueError("invalid event prune temporary")
    path = store.events_path.parent / name
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(descriptor)
        raise ValueError("invalid event prune temporary shape")
    return path, descriptor


# LLM: 只检查封存段尾，活动JSONL不参与；旧游标只清理其校验过的字面临时路径。
# 函数用途: 过期整段并消化旧pending/游标，保留原分步回执结构。
def _event_jsonl_step(store, targets, max_bytes):
    with store._connection() as conn:
        job = _read_event_step_job(conn)
        pending = conn.execute("SELECT 1 FROM metadata WHERE key=?", (_EVENTS_PRUNE_PENDING_KEY,)).fetchone()
    result = _expire_event_segments(store, max_bytes)
    if job is not None:
        write = store._local_write if store.coordinator_io else lambda operation: operation()
        # 只删除游标中通过原 basename 校验的那个临时文件，绝不按模式批量清理。
        _drop_event_step_temp(store, job, write)
    if job is not None or pending is not None:
        _event_step_transaction(store, lambda conn: _event_step_save(conn, None, targets))
    return result


def _event_segment_tail(path, max_bytes):
    """只读封存段最后至多 64KiB；不完整、过长或损坏的末行均保守保留。"""
    amount = 0
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('invalid event segment shape')
            amount = min(info.st_size, 64 * 1024)
            if amount > max_bytes:
                return None, info.st_size, 0
            offset = info.st_size - amount
            handle.seek(offset)
            tail = handle.read(amount)
        end = tail.rfind(b'\n')
        start = tail.rfind(b'\n', 0, end) + 1
        if end < 0 or end != len(tail) - 1 or (offset and start == 0):
            raise ValueError('incomplete event segment tail')
        row = json.loads(tail[start:end])
        if not isinstance(row, dict) or not isinstance(row.get('event_id'), str) or not row['event_id']:
            raise ValueError('invalid event segment tail')
        return row['event_id'], info.st_size, amount
    except (OSError, ValueError, RecursionError) as error:
        # 坏尾行也保留本次有界读取量，不把解析失败当作零成本继续下一拍。
        error.scanned_bytes = amount
        raise


def _expire_event_segments(store, max_bytes) -> dict:
    """SQL 已删掉封存段末事件才整段过期；活动文件从不打开、截断或替换。"""
    result = {"removed": 0, "removed_segments": 0, "removed_bytes": 0, "scanned_bytes": 0,
              "complete": True, "cursor": {}, "errors": []}
    paths = [path for path in iter_event_jsonl_paths(store.events_path) if path != store.events_path]
    if not paths:
        return result
    write = store._local_write if store.coordinator_io else lambda operation: operation()
    with store._connection() as conn:
        for path in paths:
            try:
                event_id, size, scanned = _event_segment_tail(path, max_bytes - result['scanned_bytes'])
            except (OSError, ValueError, RecursionError) as error:
                result['scanned_bytes'] += getattr(error, 'scanned_bytes', 0)
                result.update(complete=False, errors=[{'code': 'event_segment_tail_unreadable', 'path': path.name}])
                break
            result['scanned_bytes'] += scanned
            if event_id is None:
                result['complete'] = False
                break
            if conn.execute('SELECT 1 FROM events WHERE event_id=?', (event_id,)).fetchone() is not None:
                break
            # 连接只执行 SELECT；先关闭段 FD，再沿原写门 unlink，未持 SQLite 写锁。
            write(path.unlink)
            write(partial(_sync_parent_directory, path))
            result['removed_segments'] += 1
            result['removed_bytes'] += size
    return result


# LLM: 只有结构完整、目标未变、同dev/ino、size不小于已提交offset且前缀样本一致才续扫；否则视为换代从0重扫。
# 函数用途: 判定已保存游标能否在当前文件上续扫，返回已核对的样本窗口或None。
def _event_step_resume(job, targets, source, identity, size):
    if (job is None or not _event_step_job_valid(job) or job["targets"] != sorted(targets)
            or job["source"] != identity or size < job["offset"]):
        return None
    windows = _event_prefix_windows(source, job["offset"])
    return windows if _event_prefix_digest(*windows) == job["prefix"] else None


# LLM: 同目录mkstemp独占0600且basename受固定模式约束；新游标从0开始，只登记当前身份与原目标。
# 函数用途: 为一次完整扫描创建暂存文件与初始游标。
def _event_step_new_job(store, targets, identity, write):
    descriptor, name = write(lambda: tempfile.mkstemp(prefix=".events-prune-", suffix=".step",
                                                      dir=store.events_path.parent))
    os.close(descriptor)
    return {"source": identity, "targets": sorted(targets), "temporary": Path(name).name, "offset": 0,
            "output_offset": 0, "tail_hex": "", "removed": 0, "prefix": _event_prefix_digest(b"", b"")}


# LLM: 暂存缺失（已被原子发表消费）或短于已提交output位置都不能续写，交调用方重扫；形状不符仍按原合同拒绝。
# 函数用途: 打开续扫所需的原暂存文件，返回路径与FD或None。
def _event_step_open_temp(store, job, write):
    try:
        temporary, descriptor = write(lambda: _event_step_temp(store, job))
    except FileNotFoundError:
        return None
    if os.fstat(descriptor).st_size < job["output_offset"]:
        os.close(descriptor)
        return None
    return temporary, descriptor


# LLM: 名字不符固定模式的不是本维护创建的文件，不碰；已被原子发表消费则无需清理；形状不符仍按原合同拒绝。
# 函数用途: 放弃旧游标时删除其暂存文件。
def _drop_event_step_temp(store, job, write):
    name = job.get("temporary")
    if not isinstance(name, str) or _EVENT_STEP_TEMP_NAME.fullmatch(name) is None:
        return
    try:
        temporary, descriptor = write(lambda: _event_step_temp(store, job))
    except FileNotFoundError:
        # 原子发表后、游标确认前崩溃：原pending仍在，按新源幂等重扫，不猜发表成功。
        return
    os.close(descriptor)
    write(lambda: _discard_event_temp(temporary, None))


# LLM: 本拍新建、尚未登记游标的暂存在失败时尽力删除；清理失败不盖掉原异常，已被发表消费时无操作。
# 函数用途: 避免失败的首拍遗留无主暂存文件。
def _discard_new_event_temp(store, job, write, primary):
    temporary = store.events_path.parent / job["temporary"]
    try:
        write(lambda: _discard_event_temp(temporary, primary))
    except BaseException:
        pass


# LLM: 只处理本拍已读字节：未完整行留在游标tail_hex，final仅在锁内EOF使用；暂存先截回已提交output位置再追加并fsync。
# 函数用途: 过滤一段原始字节写入暂存文件，并推进游标offset、输出位置、计数与前缀样本。
def _event_step_consume(write, retained, job, windows, data, result, *, final):
    parts = (bytes.fromhex(job["tail_hex"]) + data).split(b"\n")
    tail = parts.pop()
    if len(tail) > 8 * 1024 * 1024:
        raise ValueError("event line exceeds bounded maintenance budget")
    lines = [part + b"\n" for part in parts]
    if final and tail:
        lines.append(tail)
        tail = b""
    target_ids = set(job["targets"])
    kept = [line for line in lines if not _pruned_event_line(line, target_ids)]

    # 读取/解析在原短门外，末写只处理本批已准备字节。
    def append():
        retained.truncate(job["output_offset"])
        retained.seek(job["output_offset"])
        retained.writelines(kept)
        retained.flush()
        os.fsync(retained.fileno())

    write(append)
    head, boundary = windows
    if len(head) < _EVENT_STEP_PROBE_BYTES:
        head = (head + data)[:_EVENT_STEP_PROBE_BYTES]
    boundary = (boundary + data)[-_EVENT_STEP_PROBE_BYTES:]
    removed = len(lines) - len(kept)
    job.update(offset=job["offset"] + len(data), output_offset=retained.tell(), tail_hex=tail.hex(),
               removed=job["removed"] + removed, prefix=_event_prefix_digest(head, boundary))
    result["removed"] += removed
    result["scanned_bytes"] += len(data)
    return head, boundary


# LLM: 仅此段持原JSONL锁（C模式由step锁持同一把锁）；核路径仍指向原inode、未截断且前缀样本未变，锁内不碰SQLite。
# 函数用途: 复核当前锁内EOF，来源换代或被改写时拒绝发表，并返回锁内字节数。
def _event_step_locked_eof(store, source, job, windows):
    identity, size = _event_file_revision(store.events_path)
    if identity != job["source"] or size < job["offset"] or _event_prefix_windows(source, job["offset"]) != windows:
        raise ValueError("event source changed during bounded scan")
    return size


# LLM: 完成态只消化旧pending/游标；新生产路径不再写部分逐行位置。
# 函数用途: 在短SQL事务里清理旧恢复账，保留原私有调用接口。
def _event_step_save(conn, job, targets):
    if job is None:
        conn.execute("DELETE FROM metadata WHERE key IN ('events_prune_cursor', ?)", (_EVENTS_PRUNE_PENDING_KEY,))
    else:
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('events_prune_cursor',?)", (json.dumps(job),))


# LLM: 调用方已BEGIN IMMEDIATE；每批900参数兼容SQLite变量上限，删除后重查避免触发器忽略的行被误清JSONL。
# 函数用途: 重核本批选择并删除，返回实际选择数量和真正从数据库消失的精确ID集合。
def _delete_prunable_events(conn, before: float, keep_last: int, max_delete: int) -> tuple[int, set[str]]:
    selected = _select_prunable_event_ids(conn, before, keep_last, max_delete)
    identifiers, deleted = sorted(selected), set()
    for offset in range(0, len(identifiers), 900):
        batch = identifiers[offset:offset + 900]
        placeholders = ",".join("?" for _ in batch)
        conn.execute(f"DELETE FROM events WHERE event_id IN ({placeholders})", batch)
        remaining = {row[0] for row in conn.execute(
            f"SELECT event_id FROM events WHERE event_id IN ({placeholders})", batch)}
        deleted.update(set(batch) - remaining)
    return len(selected), deleted


# LLM: 原始CR、坏UTF-8、坏JSON、递归超限及非字符串ID都保留；ValueError涵盖UnicodeDecodeError和JSON错误。
# 函数用途: 只删除strict UTF-8解码成功、无原始CR且精确命中目标ID的JSON对象行。
def _pruned_event_line(line: bytes, target_ids: set[str]) -> bool:
    if b"\r" in line.rstrip(b"\n"):
        return False
    try:
        row = json.loads(line.decode("utf-8", errors="strict"))
    except (ValueError, RecursionError):
        return False
    return isinstance(row, dict) and isinstance(row.get("event_id"), str) and row["event_id"] in target_ids


# LLM: deleted_jsonl沿旧键返回封存段数，不再代表行数；不把跨介质partial伪装成全事务回滚。
# 函数用途: 生成有界事件裁剪的稳定公开回执。
def _event_prune_result(selected: int, deleted: set[str], removed: int, *, dry_run: bool = False) -> dict[str, int | bool]:
    return {"ok": True, "dry_run": dry_run, "selected": selected, "deleted_db": len(deleted), "deleted_jsonl": removed}


# LLM: 正文仅活于本批调用栈，持久manifest只存代次与摘要，不保存提交来源。
# 类用途: 将有界raw快照及任务fence传入同一次短事务。
@dataclass(frozen=True)
class _FtsRebuildSnapshot:
    columns: tuple
    rows: tuple
    state: tuple
    db_identity: tuple
    contents: tuple
    versions: tuple
    content_bytes: int
    job: Any = None
    mode: str = 'copy'
    selected: tuple = ()
    tokens: tuple = ()
    advance: int | None = None
    finished: bool = False
    directory: tuple = ()
    dirty: tuple = ()


# LLM: 原schema摘要、FTS版本和pending继续参与CAS，附加真实records及捕获器结构，不把无关event当记录变化。
# 函数用途: 取得这次重建所依赖的实际库内结构状态。
def _read_rebuild_state(conn):
    rows = conn.execute(
        "SELECT key, value FROM metadata WHERE key IN (?, ?, ?) ORDER BY key",
        ("sqlite_schema_fingerprint", "records_fts_schema_version", "records_fts_rebuild_pending"),
    ).fetchall()
    names = ('records', 'records_fts', 'maintenance_fts_shadow',
             'maintenance_fts_retired_map', 'maintenance_fts_retired_map_id', *tuple(_fts_capture_sql()))
    definition = conn.execute(
        'SELECT type,name,sql FROM sqlite_master WHERE name IN ('
        + ','.join('?' for _ in names) + ') ORDER BY type,name', names).fetchall()
    return tuple(tuple(row) for row in [*rows, *definition])


# LLM: sqlite返回的每个标量原类型和值都比较，不将1/1.0、None、bytes/text或JSON字符串折叠。
# 函数用途: 判断两次完整SQL读取是否精确相同。
def _typed_rebuild_rows_equal(current, original):
    return (len(current) == len(original) and all(
        len(left) == len(right) and all(type(a) is type(b) and a == b for a, b in zip(left, right))
        for left, right in zip(current, original)))


# LLM: 仅同路径当前库文件身份用于拒绝换库，不把mtime变化或无关event当全文记录变化。
# 函数用途: 给单次重建记录真实数据库文件身份。
def _rebuild_db_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


# LLM: 精确捕获原子正文writer的实际代次，缺失/断链也有明确事实；不以None表示稳定正文。
# 函数用途: 比较原读取前后及最后事务时的正文文件状态。
def _rebuild_body_version(path: Path):
    try:
        info = path.stat()
    except FileNotFoundError:
        try:
            link = path.lstat()
        except FileNotFoundError:
            return ("missing",)
        return ("missing_target", *_rebuild_stat_generation(link))
    return ("present", *_rebuild_stat_generation(info))


# LLM: 文件代次只做变化否定，摘要来自实际读取；不宣称防住任意恶意恢复元数据的外部写者。
# 函数用途: 取得原要求的dev/ino/size/mtime/ctime版本元组。
def _rebuild_stat_generation(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: 只统计并摘要原读取输入，不改变传给FTS的实际content值或原preview fallback类型。
# 函数用途: 为本次测试/快照记录实际读取正文的字节数与摘要输入。
def _rebuild_content_bytes(content):
    if isinstance(content, bytes):
        return content
    return str(content).encode("utf-8", "replace")


# 完整驱动的单文档准入上限，不是源文件大小或RSS上限；显式step可另选预算。
_FTS_SYNC_SINGLE_MAX_BYTES = 64 * 1024 * 1024
_FTS_BATCH_LIMITS = ContextVar('local_store_fts_batch_limits', default=None)
_FTS_JOB_SQL = """CREATE TABLE maintenance_fts_job (
    slot INTEGER PRIMARY KEY CHECK(slot=1),
    job_id TEXT NOT NULL, generation INTEGER NOT NULL, phase TEXT NOT NULL,
    cursor INTEGER, upper INTEGER, ack INTEGER NOT NULL, epoch INTEGER NOT NULL,
    copied INTEGER NOT NULL, verify_cursor INTEGER, verified_epoch INTEGER,
    verified_count INTEGER NOT NULL, directory TEXT, identity TEXT NOT NULL,
    state TEXT NOT NULL)"""
_FTS_DOCS_SQL = """CREATE TABLE maintenance_fts_docs (
    rid INTEGER PRIMARY KEY, record_id UNIQUE NOT NULL, path TEXT NOT NULL,
    version TEXT NOT NULL, digest TEXT NOT NULL, bytes INTEGER NOT NULL)"""
_FTS_DELTA_SQL = """CREATE TABLE maintenance_fts_delta (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
    rid INTEGER NOT NULL)"""
_FTS_RETIRED_MAP_SQL = """CREATE TABLE maintenance_fts_retired_map (
    fts_rid INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
    epoch INTEGER NOT NULL, record_id NOT NULL)"""
_FTS_RETIRED_MAP_INDEX_SQL = """CREATE INDEX maintenance_fts_retired_map_id
    ON maintenance_fts_retired_map(job_id,epoch,record_id,fts_rid)"""
_FTS_TABLES = {'maintenance_fts_job': _FTS_JOB_SQL,
               'maintenance_fts_docs': _FTS_DOCS_SQL, 'maintenance_fts_delta': _FTS_DELTA_SQL,
               'maintenance_fts_retired_map': _FTS_RETIRED_MAP_SQL}
_FTS_PHASES = {'copy', 'delta', 'verify', 'map_gc', 'map', 'complete'}


# LLM: 初始化器只捕获OperationalError；延期维护不能变成功，也不包装撤权异常。
# 类用途: 明确标识空间、预算或不支持输入导致的维护延期。
class _FtsRebuildDeferred(sqlite3.OperationalError):
    pass


# LLM: 预算拒绝不推进cursor、不跳文档，不携带正文或提交来源。
# 类用途: 提供显式step和完整驱动都可检查的字节准入事实。
class _FtsByteBudget(_FtsRebuildDeferred):
    # LLM: 数量只描述本次准入，不能作完成或资源使用总量证明。
    # 函数用途: 保存所需字节、当前限额和拒绝阶段。
    def __init__(self, required_bytes, limit_bytes, stage):
        self.required_bytes, self.limit_bytes = int(required_bytes), int(limit_bytes)
        self.stage = stage
        super().__init__(f'fts {stage} deferred: required_bytes={self.required_bytes}, '
                         f'limit_bytes={self.limit_bytes}; single-document cap={_FTS_SYNC_SINGLE_MAX_BYTES}')


# LLM: 事务内至多256条；时间预算只在可中断的准入及循环边界生效，不是硬实时。
# 函数用途: 校验并收紧一拍重建资源预算。
def _fts_limits(max_rows, max_bytes, max_ms, max_records):
    if max_records is not None:
        if type(max_records) is not int or max_records <= 0:
            raise ValueError('max_records must be a positive integer')
        max_rows = min(max_rows, max_records)
    if type(max_rows) is not int or max_rows <= 0:
        raise ValueError('max_rows must be a positive integer')
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('max_bytes must be a positive integer')
    if isinstance(max_ms, bool) or not math.isfinite(float(max_ms)) or float(max_ms) <= 0:
        raise ValueError('max_ms must be finite and positive')
    return min(max_rows, 256), max_bytes, time.monotonic() + float(max_ms) / 1000


# LLM: 取消对象仅本次栈内使用，不保存到Store或持久账本。
# 函数用途: 在写门外响应调用方取消。
def _fts_check_cancel(cancellation):
    if cancellation is not None:
        cancelled = cancellation if type(cancellation) is bool else cancellation.is_set()
        if cancelled:
            raise _FtsRebuildDeferred('fts rebuild cancelled; committed job retained')


# LLM: 仅旁锁进入失败转换延期，yield后的正文、SQL和CAS异常保持原身份；C出生仍走原门。
# 函数用途: 串行化重建维护者而不阻止搜索。
@contextmanager
def _fts_rebuild_lock(store):
    options = {'blocking': False}
    if store.coordinator_io:
        options['birth_commit'] = store._local_write
    with ExitStack() as stack:
        try:
            stack.enter_context(locked_json_path(
                store.db_path.with_name(store.db_path.name + '.fts-rebuild'), **options))
        except BlockingIOError as exc:
            raise _FtsRebuildDeferred('fts rebuild deferred: maintenance lock busy') from exc
        yield


# LLM: C全部新写复用原端口，普通路径保持SQLite在前、事件锁由原叶取得的锁序。
# 函数用途: 执行零等待、失败不补新写的短事务。
def _fts_transaction(store, operation):
    if store.coordinator_io:
        return store._coordinator_transaction(operation, secure_delete=True)
    with store._connection() as conn:
        conn.execute('PRAGMA busy_timeout=0')
        conn.execute('PRAGMA secure_delete=ON')
        conn.execute('BEGIN IMMEDIATE')
        result = operation(conn)
        conn.commit()
        return result


# LLM: 保留SQLite标量类型，浮点、BLOB与文本不可折叠。
# 函数用途: 编码有限结构fence供恢复后精确比较。
def _fts_pack(rows):
    return json.dumps([[(type(value).__name__,
        value.hex() if isinstance(value, (bytes, float)) else value) for value in row]
        for row in rows], ensure_ascii=True, separators=(',', ':'), allow_nan=False)


# LLM: 仅归一化DDL空白，不忽略列、约束或SQL内容。
# 函数用途: 比较维护器固定结构。
def _fts_sql_key(sql):
    return ' '.join(sql.strip().rstrip(';').split())


# LLM: 只接受已知三列的内部内容FTS5，不套用外部content表或未知布局。
# 函数用途: 从active真实声明提取同样的分词器配置。
def _fts_shadow_sql(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='records_fts'").fetchone()
    declaration = row[0] if row else ''
    match = re.fullmatch(r'''CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?
        (?:"records_fts"|records_fts)\s+USING\s+fts5
        (\(\s*id\s+UNINDEXED\s*,\s*title\s*,\s*content\s*
        (?:,\s*tokenize\s*=\s*'(?:[^']|'')*'\s*)?\))\s*;?''', declaration, re.I | re.X)
    if match is None:
        raise _FtsRebuildDeferred('fts rebuild deferred: unsupported active FTS schema')
    from . import schema
    stored = schema._read_local_state(conn, schema._FTS_SCHEMA_VERSION_KEY)
    if schema._newer_fts_version(stored):
        raise _FtsRebuildDeferred('fts rebuild deferred: newer active FTS version')
    if stored != schema._FTS_SCHEMA_VERSION:
        # active仍是旧分词器；目标声明来自唯一schema，而不是复制旧布局后冒签新版本。
        return schema._FTS_TABLE_SQL.format(tokenizer=schema._FTS_TOKENIZE).replace(
            ' IF NOT EXISTS', '').replace('records_fts', 'maintenance_fts_shadow', 1)
    return 'CREATE VIRTUAL TABLE maintenance_fts_shadow USING fts5' + match.group(1)


# LLM: 最终目录fence仅覆盖规范受管原子writer，不把外部路径、链接视为同一协议。
# 函数用途: 验证单链接正文位于唯一files父目录或明确缺失。
def _fts_managed_path(store, row):
    record_id = row['id']
    if (not isinstance(record_id, str) or record_id in {'', '.', '..'}
            or any(char in record_id for char in ('/', '\\', '\x00'))):
        raise _FtsRebuildDeferred('fts rebuild deferred: unmanaged record body identity')
    path = Path(os.path.abspath(store._resolve_content_path(row['content_path'])))
    expected = Path(os.path.abspath(store._content_file(record_id)))
    if path != expected or store.files_dir.is_symlink():
        raise _FtsRebuildDeferred('fts rebuild deferred: unmanaged body path')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _FtsRebuildDeferred('fts rebuild deferred: non-atomic or linked body source')
    return path


# LLM: 所有受支持正文的实际父目录只有files_dir，门外与发表门内均核对代次。
# 函数用途: 取得该目录的实际身份和原子替换代次。
def _fts_directory(store):
    if store.files_dir.is_symlink():
        raise _FtsRebuildDeferred('fts rebuild deferred: linked files directory')
    return (str(store.files_dir.resolve()), *_rebuild_stat_generation(store.files_dir.stat()))


# LLM: copied来自持久文档计数，不是尝试次数，也不代表整个业务执行终态。
# 函数用途: 生成同一持久重建任务的真实进度回执。
def _fts_reply(job, status, progressed):
    return {'status': status, 'copied': int(job['copied']),
            'job_id': job['job_id'], 'progressed': bool(progressed)}


# LLM: BEFORE捕获REPLACE旧身份，AFTER捕获新rowid；所有UPDATE均进入同业务事务的delta。
# 函数用途: 建立持久变化捕获和影子即时擦除触发器。
def _fts_capture_sql():
    result = {}
    for suffix, event, condition, queue in (
        ('bi', 'BEFORE INSERT', 'record_id=NEW.id OR rid=NEW.rowid',
         'SELECT rowid AS rowid FROM records WHERE id=NEW.id OR rowid=NEW.rowid'),
        ('bu', 'BEFORE UPDATE', 'record_id=OLD.id OR record_id=NEW.id OR rid=OLD.rowid OR rid=NEW.rowid',
         'SELECT OLD.rowid AS rowid UNION SELECT NEW.rowid'),
        ('bd', 'BEFORE DELETE', 'record_id=OLD.id OR rid=OLD.rowid', 'SELECT OLD.rowid AS rowid'),
    ):
        name = 'maintenance_fts_' + suffix
        result[name] = f'''CREATE TRIGGER {name} {event} ON records BEGIN
            INSERT INTO maintenance_fts_delta(job_id,rid)
              SELECT j.job_id,q.rowid FROM maintenance_fts_job AS j,
              ({queue}) AS q WHERE j.slot=1 AND j.phase<>'complete';
            UPDATE maintenance_fts_job SET epoch=epoch+1,
              copied=copied-(SELECT COUNT(*) FROM maintenance_fts_docs WHERE {condition})
              WHERE slot=1 AND phase<>'complete';
            DELETE FROM maintenance_fts_shadow WHERE rowid IN
              (SELECT rid FROM maintenance_fts_docs WHERE {condition});
            DELETE FROM maintenance_fts_docs WHERE {condition};
        END'''
    for suffix, event in (('ai', 'AFTER INSERT'), ('au', 'AFTER UPDATE')):
        name = 'maintenance_fts_' + suffix
        result[name] = f'''CREATE TRIGGER {name} {event} ON records BEGIN
            INSERT INTO maintenance_fts_delta(job_id,rid)
              SELECT job_id,NEW.rowid FROM maintenance_fts_job WHERE slot=1 AND phase<>'complete';
            UPDATE maintenance_fts_job SET epoch=epoch+1 WHERE slot=1 AND phase<>'complete';
        END'''
    return result


# LLM: 退休id为UNINDEXED，先查带job/epoch/record_id索引的映射再按真实旧rowid擦除。
# 函数用途: 生成覆盖INSERT、UPDATE、DELETE及REPLACE旧身份的退休防护。
def _fts_retired_sql():
    result = {}
    for suffix, event, identifiers in (
        ('ri', 'BEFORE INSERT', 'SELECT NEW.id UNION SELECT id FROM records WHERE rowid=NEW.rowid'),
        ('ru', 'BEFORE UPDATE', 'SELECT OLD.id UNION SELECT NEW.id UNION SELECT id FROM records WHERE rowid=NEW.rowid'),
        ('rd', 'BEFORE DELETE', 'SELECT OLD.id'),
    ):
        name = 'maintenance_fts_' + suffix
        predicate = f'''job_id=(SELECT job_id FROM maintenance_fts_job WHERE slot=1)
            AND epoch=(SELECT verified_epoch FROM maintenance_fts_job WHERE slot=1)
            AND record_id IN ({identifiers})'''
        result[name] = f'''CREATE TRIGGER {name} {event} ON records BEGIN
            DELETE FROM maintenance_fts_retired WHERE rowid IN
              (SELECT fts_rid FROM maintenance_fts_retired_map
               INDEXED BY maintenance_fts_retired_map_id WHERE {predicate});
            DELETE FROM maintenance_fts_retired_map WHERE {predicate};
        END'''
    return result


# LLM: 坏DDL、缺表及捕获器漂移不允许静默接管，不丢弃旧job。
# 函数用途: 核对维护布局和单槽持久任务。
def _fts_job(conn):
    exists = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='maintenance_fts_job'").fetchone()
    if exists is None:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'maintenance_fts_*' LIMIT 1").fetchone():
            raise _FtsRebuildDeferred('invalid fts rebuild job: orphan maintenance objects')
        return None
    for name, expected in _FTS_TABLES.items():
        row = conn.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)).fetchone()
        if row is None or _fts_sql_key(row[0]) != _fts_sql_key(expected):
            raise _FtsRebuildDeferred('invalid fts rebuild job: maintenance schema changed')
    rows = conn.execute('SELECT * FROM maintenance_fts_job ORDER BY slot LIMIT 2').fetchall()
    index = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='maintenance_fts_retired_map_id'").fetchone()
    if index is None or _fts_sql_key(index[0]) != _fts_sql_key(_FTS_RETIRED_MAP_INDEX_SQL):
        raise _FtsRebuildDeferred('invalid fts rebuild job: retirement map index changed')
    if len(rows) != 1:
        raise _FtsRebuildDeferred('invalid fts rebuild job: slot count')
    job = dict(rows[0])
    integers = ('slot', 'generation', 'ack', 'epoch', 'copied', 'verified_count')
    if (any(type(job[key]) is not int or job[key] < 0 for key in integers)
            or job['slot'] != 1 or job['generation'] < 1 or job['phase'] not in _FTS_PHASES
            or not isinstance(job['job_id'], str) or re.fullmatch('[0-9a-f]{32}', job['job_id']) is None):
        raise _FtsRebuildDeferred('invalid fts rebuild job: fields')
    for key in ('cursor', 'upper', 'verify_cursor', 'verified_epoch'):
        if job[key] is not None and type(job[key]) is not int:
            raise _FtsRebuildDeferred('invalid fts rebuild job: cursor')
    if job['cursor'] is not None and (job['upper'] is None or job['cursor'] > job['upper']):
        raise _FtsRebuildDeferred('invalid fts rebuild job: upper bound')
    try:
        identity = json.loads(job['identity'])
        state = json.loads(job['state'])
        directory = None if job['directory'] is None else json.loads(job['directory'])
    except (TypeError, ValueError) as exc:
        raise _FtsRebuildDeferred('invalid fts rebuild job: encoded fence') from exc
    if (not isinstance(identity, list) or len(identity) != 2
            or any(type(value) is not int for value in identity) or not isinstance(state, list)
            or (directory is not None and (not isinstance(directory, list) or len(directory) != 6))):
        raise _FtsRebuildDeferred('invalid fts rebuild job: fence shape')
    if job['phase'] in {'verify', 'map_gc', 'map'} and (directory is None or job['verified_epoch'] is None):
        raise _FtsRebuildDeferred('invalid fts rebuild job: verification fence')
    retired = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_fts_retired'").fetchone() is not None
    shadow = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='maintenance_fts_shadow'").fetchone()
    if job['phase'] == 'complete':
        if shadow is not None:
            raise _FtsRebuildDeferred('invalid fts rebuild job: completed shadow')
        expected_triggers = _fts_retired_sql() if retired else {}
    else:
        if retired or shadow is None:
            raise _FtsRebuildDeferred('invalid fts rebuild job: generation tables')
        if _fts_sql_key(shadow[0]) != _fts_sql_key(_fts_shadow_sql(conn)):
            raise _FtsRebuildDeferred('invalid fts rebuild job: shadow schema')
        expected_triggers = _fts_capture_sql()
    actual = {row[0]: row[1] for row in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'maintenance_fts_*'")}
    if (actual.keys() != expected_triggers.keys()
            or any(_fts_sql_key(actual[name]) != _fts_sql_key(sql) for name, sql in expected_triggers.items())):
        raise _FtsRebuildDeferred('invalid fts rebuild job: capture triggers changed')
    return job


# LLM: 每个任务SQLite标量的类型和值均参与CAS，不只比较条数或hash。
# 函数用途: 判断本批任务fence是否仍是同一代。
def _fts_same_job(current, expected):
    return (current is expected if current is None or expected is None else
            current.keys() == expected.keys() and _typed_rebuild_rows_equal(
                (tuple(current.values()),), (tuple(expected.values()),)))


# LLM: 只更新固定状态字段并递增持久代次，参数不携带正文或提交来源。
# 函数用途: 原子推进任务状态。
def _fts_update(
    conn: sqlite3.Connection, *, phase: str | EllipsisType = ...,
    cursor: int | None | EllipsisType = ..., ack: int | EllipsisType = ...,
    copied: int | EllipsisType = ..., verify_cursor: int | None | EllipsisType = ...,
    verified_epoch: int | None | EllipsisType = ..., verified_count: int | EllipsisType = ...,
    directory: str | None | EllipsisType = ...,
) -> None:
    # 省略字段不改旧值；显式None仍须写SQL NULL，不能当作未提供。
    changes = {key: value for key, value in {
        'phase': phase, 'cursor': cursor, 'ack': ack, 'copied': copied,
        'verify_cursor': verify_cursor, 'verified_epoch': verified_epoch,
        'verified_count': verified_count, 'directory': directory,
    }.items() if value is not ...}
    assignments = [f'{key}=?' for key in changes]
    conn.execute('UPDATE maintenance_fts_job SET '
                 + ','.join([*assignments, 'generation=generation+1'])
                 + ' WHERE slot=1', tuple(changes.values()))


# LLM: dbstat取active实际backing页，不可用时保守取DB及WAL上界；无法估算不降成零。
# 函数用途: 在影子DDL和pending改变前预检至少1.2倍可用空间。
def _fts_check_space(store):
    estimate = page_bound = None
    with store._connection() as conn:
        _fts_shadow_sql(conn)
        try:
            page_bound = int(conn.execute('PRAGMA page_size').fetchone()[0]) * int(conn.execute('PRAGMA page_count').fetchone()[0])
        except sqlite3.Error:
            pass
        # 整库页数是影子FTS的保守上界；dbstat SUM会遍历索引全部页，不能藏在有界首拍。
        estimate = page_bound
    if estimate is None:
        try:
            database = store.db_path.stat().st_size
            try:
                wal = Path(str(store.db_path) + '-wal').stat().st_size
            except FileNotFoundError:
                wal = 0
            estimate = max(database + wal, page_bound or 0)
        except OSError as exc:
            raise _FtsRebuildDeferred('fts rebuild space estimate unavailable') from exc
        if estimate <= 0:
            raise _FtsRebuildDeferred('fts rebuild space estimate unavailable')
    try:
        info = os.statvfs(store.db_path.parent)
        free = int(info.f_bavail) * int(info.f_frsize)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise _FtsRebuildDeferred('fts rebuild free space unavailable') from exc
    if free < 0 or 5 * free < 6 * estimate:
        raise _FtsRebuildDeferred('insufficient space for shadow FTS rebuild')


# LLM: 任务、rowid固定上界、捕获器和pending同事务建立，不先扫描全库或清空active。
# 函数用途: 开始新任务或在上一代回收完毕后更换任务。
def _fts_start(store, expected):
    identity = _rebuild_db_identity(store.db_path)
    job_id = uuid.uuid4().hex

    # LLM: C仍沿原写叶；空间预检在门外，门内仅固定DDL和常量状态。
    # 函数用途: 原子安装持久任务、影子和变化捕获器。
    def operation(conn):
        current = _fts_job(conn)
        if not _fts_same_job(current, expected) or _rebuild_db_identity(store.db_path) != identity:
            raise BlockingIOError('local_store_rebuild_snapshot_changed')
        shadow_sql = _fts_shadow_sql(conn)
        if current is None:
            for statement in _FTS_TABLES.values():
                conn.execute(statement)
            conn.execute(_FTS_RETIRED_MAP_INDEX_SQL)
            generation = 1
        else:
            if current['phase'] != 'complete':
                raise BlockingIOError('local_store_rebuild_job_in_progress')
            if (conn.execute('SELECT 1 FROM maintenance_fts_docs LIMIT 1').fetchone()
                    or conn.execute('SELECT 1 FROM maintenance_fts_delta LIMIT 1').fetchone()
                    or conn.execute('SELECT 1 FROM maintenance_fts_retired_map LIMIT 1').fetchone()
                    or conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_retired'").fetchone()):
                raise BlockingIOError('local_store_rebuild_retirement_pending')
            generation = current['generation'] + 1
            conn.execute('DELETE FROM maintenance_fts_job WHERE slot=1')
        conn.execute(shadow_sql)
        upper = conn.execute('SELECT MAX(rowid) FROM records').fetchone()[0]
        conn.execute('INSERT INTO maintenance_fts_job'
            '(slot,job_id,generation,phase,cursor,upper,ack,epoch,copied,'
            'verify_cursor,verified_epoch,verified_count,directory,identity,state)'
            " VALUES(1,?,?,'copy',NULL,?,0,0,0,NULL,NULL,0,NULL,?,'[]')",
            (job_id, generation, upper, json.dumps(identity)))
        for statement in _fts_capture_sql().values():
            conn.execute(statement)
        conn.execute("INSERT INTO metadata(key,value) VALUES('records_fts_rebuild_pending','true') "
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?, 'false')", (FTS_ROWID_ALIGNED_KEY,))
        conn.execute('UPDATE maintenance_fts_job SET state=? WHERE slot=1',
                     (_fts_pack(_read_rebuild_state(conn)),))
    _fts_transaction(store, operation)
    store._fts_needs_rebuild = True


# LLM: 只查询本批rowid集合；完整列、值和SQLite类型均参与CAS。
# 函数用途: 读取一批原记录或manifest的原始快照。
def _fts_rows_for_ids(conn, identifiers, *, manifest=False):
    select = 'SELECT * FROM maintenance_fts_docs' if manifest else 'SELECT rowid AS __fts_rowid__,* FROM records'
    key = 'rid' if manifest else 'rowid'
    if identifiers:
        cursor = conn.execute(select + f' WHERE {key} IN (' + ','.join('?' for _ in identifiers)
                              + f') ORDER BY {key}', tuple(identifiers))
    else:
        cursor = conn.execute(select + ' WHERE 0')
    columns = tuple(column[0] for column in cursor.description)
    return columns, tuple(tuple(row) for row in cursor)


# LLM: 初始cursor使用NULL，负rowid及0均合法；不从头OFFSET扫描。
# 函数用途: 取得固定上界内的一个基线窗口和末尾事实。
def _fts_copy_window(conn, job, limit):
    if job['upper'] is None:
        columns, rows = _fts_rows_for_ids(conn, ())
        return columns, rows, True
    where, parameters = 'rowid<=?', [job['upper']]
    if job['cursor'] is not None:
        where += ' AND rowid>?'
        parameters.append(job['cursor'])
    cursor = conn.execute('SELECT rowid AS __fts_rowid__,* FROM records WHERE ' + where
                          + ' ORDER BY rowid LIMIT ?', (*parameters, limit))
    columns = tuple(column[0] for column in cursor.description)
    rows = tuple(tuple(row) for row in cursor)
    if not rows:
        return columns, rows, True
    more = conn.execute('SELECT 1 FROM records WHERE rowid>? AND rowid<=? LIMIT 1',
                        (rows[-1][0], job['upper'])).fetchone()
    return columns, rows, more is None


# LLM: manifest同样按rid递增分批，不把全表放到Root或内存。
# 函数用途: 取得待复核正文代次的有界窗口。
def _fts_manifest_window(conn, job, limit):
    where, parameters = '', []
    if job['verify_cursor'] is not None:
        where = ' WHERE rid>?'
        parameters.append(job['verify_cursor'])
    cursor = conn.execute('SELECT * FROM maintenance_fts_docs' + where
                          + ' ORDER BY rid LIMIT ?', (*parameters, limit))
    columns = tuple(column[0] for column in cursor.description)
    rows = tuple(tuple(row) for row in cursor)
    more = conn.execute('SELECT 1 FROM maintenance_fts_docs WHERE rid>? LIMIT 1',
                        (rows[-1][0],)).fetchone() if rows else None
    return columns, rows, more is None


# LLM: 映射取真实旧active rowid，不将新manifest rid误作旧FTS身份；只读本批有限行。
# 函数用途: 读取映射构建、回收或事务内精确CAS的窗口。
def _fts_map_rows(conn, *, identifiers=None, after=None, limit=None, cleanup=False):
    if cleanup:
        select, key = 'SELECT * FROM maintenance_fts_retired_map', 'fts_rid'
    else:
        select, key = 'SELECT rowid AS fts_rid,id AS record_id FROM records_fts', 'rowid'
    parameters = []
    if identifiers is not None:
        where = f' WHERE {key} IN (' + ','.join('?' for _ in identifiers) + ')' if identifiers else ' WHERE 0'
        parameters.extend(identifiers)
    elif after is not None:
        where = f' WHERE {key}>?'
        parameters.append(after)
    else:
        where = ''
    sql = select + where + f' ORDER BY {key}'
    if limit is not None:
        sql += ' LIMIT ?'
        parameters.append(limit)
    cursor = conn.execute(sql, tuple(parameters))
    columns, rows = tuple(column[0] for column in cursor.description), tuple(tuple(row) for row in cursor)
    if identifiers is not None:
        return columns, rows
    more = conn.execute(select + f' WHERE {key}>? LIMIT 1', (rows[-1][0],)).fetchone() if rows else None
    return columns, rows, more is None


# LLM: stat、正文、摘要与映射读取均在门外，保留原读取叶及preview语义。
# 函数用途: 准备基线、delta、manifest或退休映射的完整有限批次。
def _fts_prepare(store):
    limits = _FTS_BATCH_LIMITS.get()
    if limits is None:
        limits = _fts_limits(256, 8 * 1024 * 1024, 1000, None)
    max_rows, max_bytes, deadline = limits
    identity = _rebuild_db_identity(store.db_path)
    directory = _fts_directory(store)
    tokens = ()
    with store._connection() as conn:
        job = _fts_job(conn)
        state = _read_rebuild_state(conn)
        if job is not None and job['phase'] != 'complete':
            if _fts_pack(state) != job['state'] or json.loads(job['identity']) != list(identity):
                raise _FtsRebuildDeferred('fts rebuild deferred: stored structure fence changed')
            mode = job['phase']
        else:
            # 直接调用旧准备接口仍读取真实正文，但不创建维护任务。
            job, mode = None, 'copy'
        if mode == 'copy':
            bounds = job if job is not None else {'cursor': None,
                'upper': conn.execute('SELECT MAX(rowid) FROM records').fetchone()[0]}
            columns, candidates, finished = _fts_copy_window(conn, bounds, max_rows)
        elif mode == 'delta':
            tokens = tuple(tuple(row) for row in conn.execute(
                'SELECT seq,rid FROM maintenance_fts_delta WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?',
                (job['job_id'], job['ack'], max_rows)))
            columns, candidates = _fts_rows_for_ids(conn, tuple(dict.fromkeys(row[1] for row in tokens)))
            finished = not tokens
        else:
            if job['verified_epoch'] != job['epoch']:
                mode, columns, candidates, finished = 'rewind', (), (), True
            elif tuple(json.loads(job['directory'])) != directory:
                mode, columns, candidates, finished = 'restart', (), (), True
            else:
                if mode in {'map_gc', 'map'}:
                    columns, candidates, finished = _fts_map_rows(conn,
                        after=job['verify_cursor'] if mode == 'map' else None,
                        limit=max_rows, cleanup=mode == 'map_gc')
                else:
                    columns, candidates, finished = _fts_manifest_window(conn, job, max_rows)
    if _rebuild_db_identity(store.db_path) != identity:
        raise BlockingIOError('local_store_rebuild_database_changed')
    if mode in {'rewind', 'restart'}:
        return _FtsRebuildSnapshot(columns, (), state, identity, (), (), 0,
            job=job, mode=mode, finished=True, directory=directory)
    if mode in {'map_gc', 'map'}:
        rows, selected, consumed = [], [], 0
        for values in candidates:
            if time.monotonic() >= deadline:
                break
            cost = sum(len(_rebuild_content_bytes(value)) for value in values)
            if consumed + cost > max_bytes:
                if not selected:
                    raise _FtsByteBudget(cost, max_bytes, mode)
                break
            rows.append(values)
            selected.append(values[0])
            consumed += cost
        if _fts_directory(store) != directory:
            raise BlockingIOError('local_store_rebuild_content_directory_changed')
        return _FtsRebuildSnapshot(columns, tuple(rows), state, identity, (), (), 0,
            job=job, mode=mode, selected=tuple(selected),
            advance=selected[-1] if selected else job['verify_cursor'],
            finished=finished and len(rows) == len(candidates), directory=directory)
    if mode == 'verify':
        rows, versions, selected, dirty = [], [], [], []
        consumed = 0
        for values in candidates:
            if time.monotonic() >= deadline:
                break
            cost = sum(len(_rebuild_content_bytes(value)) for value in values)
            if consumed + cost > max_bytes:
                if not selected:
                    raise _FtsByteBudget(cost, max_bytes, 'manifest')
                break
            entry = dict(zip(columns, values))
            path = _fts_managed_path(store, {'id': entry['record_id'], 'content_path': entry['path']})
            version = _rebuild_body_version(path)
            try:
                expected_version = tuple(json.loads(entry['version']))
            except (TypeError, ValueError) as exc:
                raise _FtsRebuildDeferred('invalid fts manifest version') from exc
            if version != expected_version:
                dirty.append(entry['rid'])
            rows.append(values)
            selected.append(entry['rid'])
            versions.append((path, version, entry['digest']))
            consumed += cost
        if _fts_directory(store) != directory:
            raise BlockingIOError('local_store_rebuild_content_directory_changed')
        return _FtsRebuildSnapshot(columns, tuple(rows), state, identity, (), tuple(versions), 0,
            job=job, mode=mode, selected=tuple(selected),
            advance=selected[-1] if selected else job['verify_cursor'],
            finished=finished and len(rows) == len(candidates), directory=directory, dirty=tuple(dirty))
    by_id = {values[0]: values for values in candidates}
    sequence = tokens if mode == 'delta' else tuple((values[0], values[0]) for values in candidates)
    selected, processed, contents, versions = [], [], [], []
    content_bytes = consumed = 0
    admitted_tokens = []
    for token_value, identifier in sequence:
        if time.monotonic() >= deadline:
            break
        if identifier in selected:
            admitted_tokens.append((token_value, identifier))
            continue
        values = by_id.get(identifier)
        if values is None:
            selected.append(identifier)
            admitted_tokens.append((token_value, identifier))
            continue
        row = dict(zip(columns, values))
        path = _fts_managed_path(store, row)
        before = _rebuild_body_version(path)
        row_cost = sum(len(_rebuild_content_bytes(value)) for value in values)
        # replacement解码的每个源字节最多成为三个UTF8字节，先限制原完整读取叶。
        body_bound = (3 * path.stat().st_size if before[0] == 'present'
                      else len(_rebuild_content_bytes(row['content_preview'])))
        if consumed + row_cost + body_bound > max_bytes:
            if not selected:
                raise _FtsByteBudget(row_cost + body_bound, max_bytes, 'body')
            break
        content = store._read_content(row)
        if _rebuild_body_version(path) != before:
            raise BlockingIOError('local_store_rebuild_content_changed')
        raw = _rebuild_content_bytes(content)
        if consumed + row_cost + len(raw) > max_bytes:
            raise _FtsRebuildDeferred('fts rebuild deferred: body grew beyond byte budget')
        selected.append(identifier)
        processed.append(values)
        admitted_tokens.append((token_value, identifier))
        contents.append((row['id'], row['title'], content))
        versions.append((path, before, hashlib.sha256(raw).hexdigest()))
        consumed += row_cost + len(raw)
        content_bytes += len(raw)
    if _rebuild_db_identity(store.db_path) != identity:
        raise BlockingIOError('local_store_rebuild_database_changed')
    if mode == 'copy':
        advance = selected[-1] if selected else (job['cursor'] if job else None)
        done = finished and len(admitted_tokens) == len(sequence)
        saved_tokens = ()
    else:
        advance = admitted_tokens[-1][0] if admitted_tokens else job['ack']
        done, saved_tokens = not tokens, tuple(admitted_tokens)
    return _FtsRebuildSnapshot(columns, tuple(sorted(processed, key=lambda values: values[0])),
        state, identity, tuple(contents), tuple(versions), content_bytes,
        job=job, mode=mode, selected=tuple(sorted(selected)), tokens=saved_tokens,
        advance=advance, finished=done, directory=directory)


# LLM: 保留完整raw行、任务及本批正文代次CAS；最终只作有限目录stat，不在写门读正文。
# 函数用途: 拒绝准备后的库、结构、记录、delta或正文变化。
def _fts_validate(store, conn, snapshot):
    # 先拒绝准备后版本/DDL变化；不能让新版本判定遮住原快照CAS异常。
    if snapshot.job is None or not _typed_rebuild_rows_equal(_read_rebuild_state(conn), snapshot.state):
        raise BlockingIOError('local_store_rebuild_snapshot_changed')
    job = _fts_job(conn)
    if (not _fts_same_job(job, snapshot.job)
            or _rebuild_db_identity(store.db_path) != snapshot.db_identity
            or json.loads(job['identity']) != list(snapshot.db_identity)
            or _fts_pack(snapshot.state) != job['state']):
        raise BlockingIOError('local_store_rebuild_snapshot_changed')
    if snapshot.columns:
        if snapshot.mode in {'map_gc', 'map'}:
            columns, rows = _fts_map_rows(conn, identifiers=snapshot.selected, cleanup=snapshot.mode == 'map_gc')
        else:
            columns, rows = _fts_rows_for_ids(conn, snapshot.selected, manifest=snapshot.mode == 'verify')
        if columns != snapshot.columns or not _typed_rebuild_rows_equal(rows, snapshot.rows):
            raise BlockingIOError('local_store_rebuild_snapshot_changed')
    if snapshot.mode == 'delta':
        tokens = tuple(tuple(row) for row in conn.execute(
            'SELECT seq,rid FROM maintenance_fts_delta WHERE job_id=? AND seq>? AND seq<=? ORDER BY seq',
            (job['job_id'], job['ack'], snapshot.advance)))
        if not _typed_rebuild_rows_equal(tokens, snapshot.tokens):
            raise BlockingIOError('local_store_rebuild_delta_changed')
        if snapshot.finished and conn.execute(
                'SELECT 1 FROM maintenance_fts_delta WHERE job_id=? AND seq>? LIMIT 1',
                (job['job_id'], job['ack'])).fetchone():
            raise BlockingIOError('local_store_rebuild_delta_changed')
    for path, version, _digest in snapshot.versions:
        if _rebuild_body_version(path) != version:
            raise BlockingIOError('local_store_rebuild_content_changed')
    if snapshot.mode in {'verify', 'map_gc', 'map', 'restart', 'rewind'} or (snapshot.mode == 'delta' and snapshot.finished):
        if _fts_directory(store) != snapshot.directory:
            raise BlockingIOError('local_store_rebuild_content_directory_changed')


# LLM: 仅适配原_replace_fts_row的两条固定SQL，保留真实故障注入路径，不提供active回退。
# 类用途: 将原写叶绑定到影子及本条records rowid。
class _FtsShadowConnection:
    # LLM: 包装仅存本次事务栈，不泄漏连接或提交来源。
    # 函数用途: 绑定当前影子文档rowid。
    def __init__(self, conn, rid):
        self.conn, self.rid, self.inserted = conn, rid, False

    # LLM: 不作通用SQL替换，固定原叶之外立即拒绝。
    # 函数用途: 精确转发原删除和插入到影子表。
    def execute(self, sql, parameters=()):
        normalized = ' '.join(sql.split())
        if normalized == 'DELETE FROM records_fts WHERE id = ?':
            # id冲突已由有唯一索引的manifest处理；此叶只能删除当前绑定rowid，避免UNINDEXED列全扫。
            return self.conn.execute('DELETE FROM maintenance_fts_shadow WHERE rowid = ?', (self.rid,))
        if normalized == 'INSERT INTO records_fts(id, title, content) VALUES (?, ?, ?)':
            cursor = self.conn.execute('INSERT INTO maintenance_fts_shadow(rowid,id,title,content) VALUES(?,?,?,?)',
                                       (self.rid, *parameters))
            self.inserted = True
            return cursor
        raise ValueError('unsupported SQL in FTS shadow adapter')


# LLM: 映射、正文版本和影子同事务，copied保持真实当前文档计数。
# 函数用途: 通过原真实写叶替换一条影子文档。
def _fts_write_doc(store, conn, values, columns, content, version):
    row = dict(zip(columns, values))
    rid = values[0]
    existing = tuple(item[0] for item in conn.execute(
        'SELECT rid FROM maintenance_fts_docs WHERE rid=? OR record_id=?', (rid, row['id'])))
    for old_rid in existing:
        conn.execute('DELETE FROM maintenance_fts_shadow WHERE rowid=?', (old_rid,))
        conn.execute('DELETE FROM maintenance_fts_docs WHERE rid=?', (old_rid,))
    adapter = _FtsShadowConnection(conn, rid)
    store._replace_fts_row(adapter, row['id'], row['title'], content)
    if not adapter.inserted:
        raise _FtsRebuildDeferred('fts shadow writer did not insert a document')
    path, body_version, digest = version
    conn.execute('INSERT INTO maintenance_fts_docs(rid,record_id,path,version,digest,bytes) VALUES(?,?,?,?,?,?)',
        (rid, row['id'], str(path), json.dumps(body_version), digest, len(_rebuild_content_bytes(content))))
    conn.execute('UPDATE maintenance_fts_job SET copied=copied+? WHERE slot=1', (1 - len(existing),))


# LLM: 缺失和rowid复用以当前raw记录为准，ack与本批影子变化同事务。
# 函数用途: 应用本批正文及删除映射。
def _fts_apply_documents(store, conn, snapshot):
    actual = {values[0] for values in snapshot.rows}
    for rid in snapshot.selected:
        if rid not in actual:
            removed = conn.execute('SELECT 1 FROM maintenance_fts_docs WHERE rid=?', (rid,)).fetchone() is not None
            conn.execute('DELETE FROM maintenance_fts_shadow WHERE rowid=?', (rid,))
            conn.execute('DELETE FROM maintenance_fts_docs WHERE rid=?', (rid,))
            if removed:
                conn.execute('UPDATE maintenance_fts_job SET copied=copied-1 WHERE slot=1')
    content_by_id = {record_id: (content, version) for (record_id, _title, content), version
                     in zip(snapshot.contents, snapshot.versions)}
    for values in snapshot.rows:
        row = dict(zip(snapshot.columns, values))
        content, version = content_by_id[row['id']]
        _fts_write_doc(store, conn, values, snapshot.columns, content, version)


# LLM: cursor只推进真实提交；verify与map共用epoch/目录fence，旧映射先有界回收。
# 函数用途: 提交基线、delta、验证、映射及发表的状态转换。
def _fts_apply(store, conn, snapshot):
    job = snapshot.job
    if snapshot.mode == 'copy':
        _fts_apply_documents(store, conn, snapshot)
        _fts_update(conn, cursor=job['upper'] if snapshot.finished else snapshot.advance,
                    phase='delta' if snapshot.finished else 'copy')
    elif snapshot.mode == 'delta':
        _fts_apply_documents(store, conn, snapshot)
        for seq, _rid in snapshot.tokens:
            conn.execute('DELETE FROM maintenance_fts_delta WHERE seq=? AND job_id=?', (seq, job['job_id']))
        if snapshot.finished:
            _fts_update(conn, ack=snapshot.advance, phase='verify', verify_cursor=None,
                        verified_count=0, verified_epoch=job['epoch'], directory=json.dumps(snapshot.directory))
        else:
            _fts_update(conn, ack=snapshot.advance)
    elif snapshot.mode == 'rewind':
        _fts_update(conn, phase='delta', verify_cursor=None, verified_count=0, verified_epoch=None, directory=None)
    elif snapshot.mode == 'restart':
        _fts_update(conn, phase='verify', verify_cursor=None, verified_count=0,
                    verified_epoch=job['epoch'], directory=json.dumps(snapshot.directory))
    elif snapshot.mode == 'map_gc':
        for fts_rid in snapshot.selected:
            conn.execute('DELETE FROM maintenance_fts_retired_map WHERE fts_rid=?', (fts_rid,))
        _fts_update(conn, phase='map' if snapshot.finished else 'map_gc', verify_cursor=None)
    elif snapshot.mode == 'map':
        for fts_rid, record_id in snapshot.rows:
            conn.execute('INSERT INTO maintenance_fts_retired_map(fts_rid,job_id,epoch,record_id) VALUES(?,?,?,?)',
                         (fts_rid, job['job_id'], job['verified_epoch'], record_id))
        if snapshot.finished:
            return _fts_publish(store, conn, snapshot)
        _fts_update(conn, verify_cursor=snapshot.advance)
    elif snapshot.dirty:
        for rid in snapshot.dirty:
            conn.execute('INSERT INTO maintenance_fts_delta(job_id,rid) VALUES(?,?)', (job['job_id'], rid))
        _fts_update(conn, phase='delta', verify_cursor=None, verified_count=0, verified_epoch=None, directory=None)
    elif snapshot.finished:
        _fts_update(conn, phase='map_gc', verify_cursor=None,
                    verified_count=job['verified_count'] + len(snapshot.selected))
    else:
        _fts_update(conn, verify_cursor=snapshot.advance,
                    verified_count=job['verified_count'] + len(snapshot.selected))
    return _fts_reply(_fts_job(conn), 'partial', True)


# LLM: 发表仅写最后有限映射批次，无全表INSERT SELECT；换名、事件、完成键及退休防护同提交。
# 函数用途: 在正文和真实旧active映射的共同fence闭合后原子发表。
def _fts_publish(store, conn, snapshot):
    job = snapshot.job
    if (job['phase'] != 'map' or job['verified_epoch'] != job['epoch']
            or tuple(json.loads(job['directory'])) != snapshot.directory
            or job['verified_count'] != job['copied']
            or conn.execute('SELECT 1 FROM maintenance_fts_delta WHERE job_id=? AND seq>? LIMIT 1',
                            (job['job_id'], job['ack'])).fetchone()):
        raise BlockingIOError('local_store_rebuild_publish_fence_changed')
    if _fts_directory(store) != snapshot.directory:
        raise BlockingIOError('local_store_rebuild_content_directory_changed')
    for name in _fts_capture_sql():
        conn.execute('DROP TRIGGER ' + name)
    conn.execute('ALTER TABLE records_fts RENAME TO maintenance_fts_retired')
    conn.execute('ALTER TABLE maintenance_fts_shadow RENAME TO records_fts')
    for statement in _fts_retired_sql().values():
        conn.execute(statement)
    store._record_event(conn, 'fts_rebuilt', '', {'count': job['copied']},
        **({'append_records': store._append_coordinator_event_records} if store.coordinator_io else {}))
    store._record_fts_schema_version(conn)
    store._record_fts_rebuild_complete(conn)
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?, 'true')", (FTS_ROWID_ALIGNED_KEY,))
    _fts_update(conn, phase='complete', verify_cursor=snapshot.advance, verified_count=job['copied'])
    if _fts_directory(store) != snapshot.directory:
        raise BlockingIOError('local_store_rebuild_content_directory_changed')
    return _fts_reply(_fts_job(conn), 'complete', True)


# LLM: 退休副本在发表外按预算删除，映射随文档主键擦除，剩余映射亦分批回收。
# 函数用途: 回收退休索引、manifest、delta和映射，为新重建腾位。
def _fts_cleanup_locked(store, limits):
    max_rows, max_bytes, deadline = limits
    with store._connection() as conn:
        job = _fts_job(conn)
        if job is None:
            return {'removed': 0, 'pending': False}
        if job['phase'] != 'complete':
            raise _FtsRebuildDeferred('fts retirement deferred: rebuild still active')
        retired = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_fts_retired'").fetchone() is not None
        candidates = (tuple(tuple(row) for row in conn.execute(
            'SELECT rowid,COALESCE(length(CAST(title AS BLOB)),0)'
            '+COALESCE(length(CAST(content AS BLOB)),0) FROM maintenance_fts_retired ORDER BY rowid LIMIT ?',
            (max_rows,))) if retired else ())
        retired_ids, consumed = [], 0
        for rid, size in candidates:
            if time.monotonic() >= deadline:
                break
            if consumed + size > max_bytes:
                if not retired_ids:
                    raise _FtsByteBudget(size, max_bytes, 'retired')
                break
            retired_ids.append(rid)
            consumed += size
        remaining = max_rows - len(retired_ids)
        docs = (tuple(row[0] for row in conn.execute(
            'SELECT rid FROM maintenance_fts_docs ORDER BY rid LIMIT ?', (remaining,)))
            if remaining and time.monotonic() < deadline else ())
        remaining -= len(docs)
        deltas = (tuple(row[0] for row in conn.execute(
            'SELECT seq FROM maintenance_fts_delta ORDER BY seq LIMIT ?', (remaining,)))
            if remaining and time.monotonic() < deadline else ())
        remaining -= len(deltas)
        maps = (tuple(row[0] for row in conn.execute(
            'SELECT fts_rid FROM maintenance_fts_retired_map ORDER BY fts_rid LIMIT ?', (remaining,)))
            if remaining and time.monotonic() < deadline else ())
        has_docs = conn.execute('SELECT 1 FROM maintenance_fts_docs LIMIT 1').fetchone() is not None
        has_deltas = conn.execute('SELECT 1 FROM maintenance_fts_delta LIMIT 1').fetchone() is not None
        has_maps = conn.execute('SELECT 1 FROM maintenance_fts_retired_map LIMIT 1').fetchone() is not None
    if not retired and not has_docs and not has_deltas and not has_maps:
        return {'removed': 0, 'pending': False}
    if ((candidates and not retired_ids) or
            (not retired_ids and not docs and not deltas and not maps and time.monotonic() >= deadline)):
        return {'removed': 0, 'pending': True}

    # LLM: 捕获器只能缩小旧副本；重复删除幂等，异常不在事务外补清账。
    # 函数用途: 提交已准备的一批回收，空表DROP不属于发表事务。
    def operation(conn):
        if not _fts_same_job(_fts_job(conn), job):
            raise BlockingIOError('local_store_rebuild_retirement_changed')
        removed = 0
        for rid in retired_ids:
            cursor = conn.execute('DELETE FROM maintenance_fts_retired WHERE rowid=?', (rid,))
            removed += max(0, cursor.rowcount)
            conn.execute('DELETE FROM maintenance_fts_retired_map WHERE fts_rid=?', (rid,))
        for rid in docs:
            cursor = conn.execute('DELETE FROM maintenance_fts_docs WHERE rid=?', (rid,))
            removed += max(0, cursor.rowcount)
        for seq in deltas:
            cursor = conn.execute('DELETE FROM maintenance_fts_delta WHERE seq=?', (seq,))
            removed += max(0, cursor.rowcount)
        for fts_rid in maps:
            cursor = conn.execute('DELETE FROM maintenance_fts_retired_map WHERE fts_rid=?', (fts_rid,))
            removed += max(0, cursor.rowcount)
        still_retired = False
        if retired:
            still_retired = conn.execute('SELECT 1 FROM maintenance_fts_retired LIMIT 1').fetchone() is not None
            if not still_retired:
                for name in _fts_retired_sql():
                    conn.execute('DROP TRIGGER ' + name)
                # backing segments的DROP耗时不受max_ms硬限制；明确留在发表事务外。
                conn.execute('DROP TABLE maintenance_fts_retired')
        pending = bool(still_retired or conn.execute('SELECT 1 FROM maintenance_fts_docs LIMIT 1').fetchone()
                       or conn.execute('SELECT 1 FROM maintenance_fts_delta LIMIT 1').fetchone()
                       or conn.execute('SELECT 1 FROM maintenance_fts_retired_map LIMIT 1').fetchone())
        return {'removed': removed, 'pending': pending}
    return _fts_transaction(store, operation)
