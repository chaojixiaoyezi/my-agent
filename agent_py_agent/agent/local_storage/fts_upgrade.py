"""沿原owner维护推进FTS投影升级；正文、重建job及发表仍由LocalStore持有。"""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

from ..common.global_write_commit import global_write_commit_scope
from ..user_space.directory_paging import directory_page

# 与原owner维护轮询同一节流；不建立线程、队列或另一个重建checkpoint。
FTS_UPGRADE_INTERVAL_SECONDS = 60
_FIXED = ('data/local_store.sqlite3', 'data/local_store/local.db', 'local_store/local.db')
_WORKSPACES = ('workspace', 'runtime', 'workspaces')


def next_upgrade_at(home: Path, previous: dict, now: float) -> float | None:
    """只做固定数量路径事实检查，不打开数据库或枚举workspace。"""
    from ..user_space.owner_maintenance import _local_store_candidate_exists, _real_owner_directory

    if not any(_local_store_candidate_exists(home, home / name, include_event_files=False) for name in _FIXED):
        if not _real_owner_directory(home, home.joinpath(*_WORKSPACES)):
            return None
    saved = previous.get('local_store_fts_upgrade', {})
    if not isinstance(saved, dict):
        raise ValueError('invalid FTS maintenance state')
    due = saved.get('next_at', 0)
    if type(due) not in (int, float) or not math.isfinite(due) or due < 0:
        raise ValueError('invalid FTS maintenance deadline')
    return max(float(due), 0.0)


def _candidate(home, cursor):
    """最多核三个固定库和一个目录项；游标仅负责发现，job仍在原库内。"""
    from ..user_space.owner_maintenance import _local_store_candidate_exists

    selected = cursor.pop('selected', None)
    if selected is not None:
        if not isinstance(selected, str):
            raise ValueError('invalid FTS maintenance selection')
        parts = Path(selected).parts
        if selected not in _FIXED and not (
                len(parts) == 6 and parts[:3] == _WORKSPACES
                and parts[4:] == ('local_store', 'local.db') and parts[3] not in ('', '.', '..')
                and not any(char in parts[3] for char in ('/', '\\', '\x00'))):
            raise ValueError('invalid FTS maintenance selection')
        path = home / selected
        if _local_store_candidate_exists(home, path, include_event_files=False):
            return path
        cursor.pop('selected', None)
    position = cursor.get('fixed', 0)
    if type(position) is not int or not 0 <= position <= len(_FIXED):
        raise ValueError('invalid FTS maintenance position')
    for index in range(position, len(_FIXED)):
        cursor['fixed'] = index + 1
        path = home / _FIXED[index]
        if _local_store_candidate_exists(home, path, include_event_files=False):
            cursor['selected'] = _FIXED[index]
            return path
    names, state, complete = directory_page(home, _WORKSPACES, cursor.get('directory', {}), 1,
                                             require_dir_fd=False)
    cursor['directory'] = state
    if complete:
        cursor.pop('directory', None)
        cursor['fixed'] = 0
    for name in names:
        path = home.joinpath(*_WORKSPACES, name, 'local_store', 'local.db')
        if _local_store_candidate_exists(home, path, include_event_files=False):
            cursor['selected'] = path.relative_to(home).as_posix()
            return path
    return None


def advance_upgrade(home, previous, now, *, commit=None):
    """一拍只推进一个原job；空间/锁延期保留旧索引，不影响删除维护的授权与公平性。"""
    from contextlib import nullcontext
    from copy import deepcopy

    from ..user_space.storage_lanes import (
        STORAGE_LANE_SLICE_BYTES,
        STORAGE_LANE_SLICE_ENTRIES,
        STORAGE_LANE_SLICE_MS,
    )
    from . import LocalStore
    from .maintenance import (
        _FTS_BATCH_LIMITS,
        _FTS_SYNC_SINGLE_MAX_BYTES,
        _fts_job,
        _FtsByteBudget,
        _FtsRebuildDeferred,
        _local_events_alignment_needed,
    )

    deadline = time.monotonic() + STORAGE_LANE_SLICE_MS / 1000
    due = next_upgrade_at(home, previous, now)
    if due is None or now < due:
        return None
    saved = previous.get('local_store_fts_upgrade', {})
    cursor = deepcopy(saved.get('cursor', {}))
    if not isinstance(cursor, dict):
        raise ValueError('invalid FTS maintenance cursor')
    pending = cursor.get('pending', [])
    if (not isinstance(pending, list) or len(pending) > 8
            or any(not isinstance(path, str) for path in pending)
            or len(set(pending)) != len(pending)):
        raise ValueError('invalid FTS maintenance pending cursor')
    pending = list(pending)
    discover = cursor.get('discover_next', True)
    if type(discover) is not bool:
        raise ValueError('invalid FTS maintenance discovery turn')
    cursor.pop('selected', None)
    if pending and not discover:
        path = _candidate(home, {'selected': pending.pop(0)})
        cursor['discover_next'] = True
    else:
        path = _candidate(home, cursor)
        cursor['discover_next'] = False
    cursor['pending'] = pending
    report = {'cursor': cursor, 'next_at': now + FTS_UPGRADE_INTERVAL_SECONDS,
              'scope': 'selected_store' if path is not None else 'discovery_page',
              'status': 'discovery', 'complete': False}
    if path is None:
        return report
    relative = path.relative_to(home).as_posix()
    report['store'] = relative
    pending[:] = [entry for entry in pending if entry != relative]
    source_failure = []
    original_commit = commit
    def guarded_commit(operation):
        failures = []
        def run():
            try:
                return operation()
            except BaseException as exc:
                failures.append(exc)
                raise
        try:
            return original_commit(run)
        except BaseException as exc:
            if not failures or failures[0] is not exc:
                source_failure.append(exc)
            raise
    if commit is not None:
        commit = guarded_commit
    try:
        # 只读预检不构造Store、不做schema迁移；已对齐且没有退休责任的库直接移向下一项。
        from ..io.sqlite_scope import connect_projection_sqlite, scoped_connection
        from .schema import _LOCAL_CONNECTION_TIMEOUT, _retire_coordinator_connection

        def read_connection():
            conn = connect_projection_sqlite(path, read_only=True, timeout=0)
            try:
                conn.row_factory = sqlite3.Row
                return conn
            except BaseException as exc:
                _retire_coordinator_connection(conn, exc)
                raise
        with scoped_connection(path, True, read_connection) as conn:
            needed = _local_events_alignment_needed(conn)
            job = _fts_job(conn)
            retired = conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_retired'").fetchone() is not None
        if not needed and not retired:
            cursor.pop('selected', None)
            return {**report, 'status': 'current', 'complete': True}
        if time.monotonic() >= deadline:
            return {**report, 'status': 'budget_exhausted'}
        scope = global_write_commit_scope(commit) if commit is not None else nullcontext()
        timeout_token = _LOCAL_CONNECTION_TIMEOUT.set(0.0)
        try:
            with scope:
                # 即使schema恢复pending，构造也不能偷偷同步完成重建。
                token = _FTS_BATCH_LIMITS.set((STORAGE_LANE_SLICE_ENTRIES, STORAGE_LANE_SLICE_BYTES, deadline))
                try:
                    store = LocalStore(path, coordinator_io=commit is not None)
                finally:
                    _FTS_BATCH_LIMITS.reset(token)
                try:
                    remaining_ms = max(0, (deadline - time.monotonic()) * 1000)
                    if remaining_ms <= 0:
                        return {**report, 'status': 'budget_exhausted'}
                    if needed:
                        result = store.rebuild_fts_step(max_rows=STORAGE_LANE_SLICE_ENTRIES,
                            max_bytes=STORAGE_LANE_SLICE_BYTES, max_ms=remaining_ms,
                            start_new=job is None or job['phase'] == 'complete')
                        report.update(status=result['status'], copied=result['copied'])
                    else:
                        result = store.cleanup_fts_retired_step(max_rows=STORAGE_LANE_SLICE_ENTRIES,
                            max_bytes=STORAGE_LANE_SLICE_BYTES, max_ms=remaining_ms)
                        report.update(status='retiring' if result['pending'] else 'current', complete=not result['pending'])
                        if report['complete']:
                            cursor.pop('selected', None)
                except _FtsByteBudget as exc:
                    remaining_ms = max(0, (deadline - time.monotonic()) * 1000)
                    if exc.required_bytes <= _FTS_SYNC_SINGLE_MAX_BYTES and remaining_ms > 0:
                        # 复用同步驱动原单文档准入，但不重置整拍deadline、不循环整库。
                        if needed:
                            result = store.rebuild_fts_step(max_rows=1, max_bytes=exc.required_bytes,
                                max_ms=remaining_ms, start_new=False)
                            report.update(status=result['status'], copied=result['copied'],
                                          single_document_bytes=exc.required_bytes)
                        else:
                            result = store.cleanup_fts_retired_step(max_rows=1, max_bytes=exc.required_bytes,
                                max_ms=remaining_ms)
                            report.update(status='retiring' if result['pending'] else 'current',
                                          complete=not result['pending'], single_document_bytes=exc.required_bytes)
                    elif exc.required_bytes <= _FTS_SYNC_SINGLE_MAX_BYTES:
                        report.update(status='budget_exhausted', required_bytes=exc.required_bytes)
                    else:
                        report.update(status='unsupported_document', required_bytes=exc.required_bytes,
                                      limit_bytes=_FTS_SYNC_SINGLE_MAX_BYTES)
                except _FtsRebuildDeferred as exc:
                    report.update(status='deferred', reason=str(exc))
            if report['status'] in {'partial', 'complete', 'retiring', 'budget_exhausted'}:
                relative = path.relative_to(home).as_posix()
                if relative not in pending and len(pending) < 8:
                    pending.append(relative)
            cursor.pop('selected', None)
            return report
        finally:
            _LOCAL_CONNECTION_TIMEOUT.reset(timeout_token)
    except (sqlite3.Error, OSError) as exc:
        if source_failure and exc is source_failure[0]:
            raise
        cursor.pop('selected', None)
        return {**report, 'status': 'unknown', 'error_type': type(exc).__name__}
