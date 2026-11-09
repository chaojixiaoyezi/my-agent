"""沿原owner维护推进FTS投影升级；正文、重建job及发表仍由LocalStore持有。"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from ..common.global_write_commit import global_write_commit_scope
from ..io.sqlite_scope import sqlite_connection_scope
from ..user_space.directory_paging import directory_page

# 与原owner维护轮询同一节流；不建立线程、队列或另一个重建checkpoint。
FTS_UPGRADE_INTERVAL_SECONDS = 60
# 单owner一次追赶最多调用的有界片数；仍同时受单owner时间和整拍预算约束。
FTS_UPGRADE_CATCHUP_MAX_STEPS_COUNT = 1024
# 单owner一次追赶的合作式毫秒预算；每片释放连接和业务锁后再续。
FTS_UPGRADE_CATCHUP_MAX_MS = 2000
# 原维护整拍跨owner共享的最大有界片数，防止各owner重复获得整拍额度。
FTS_UPGRADE_TICK_MAX_STEPS_COUNT = 4096


@dataclass
class FtsUpgradeTickBudget:
    deadline: float
    remaining_steps: int
    parent: FtsUpgradeTickBudget | None = None
    pid: int = field(default_factory=os.getpid)
    closed: bool = False
    lock: Lock = field(default_factory=Lock)

    def reserve(self) -> bool:
        if self.pid != os.getpid():
            return False
        with self.lock:
            if (self.closed or self.pid != os.getpid() or self.remaining_steps <= 0
                    or time.monotonic() >= self.deadline):
                return False
            if self.parent is not None and not self.parent.reserve():
                return False
            self.remaining_steps -= 1
            return True

    def exhausted(self) -> bool:
        if self.pid != os.getpid():
            return True
        with self.lock:
            unavailable = (self.closed or self.pid != os.getpid() or self.remaining_steps <= 0
                           or time.monotonic() >= self.deadline)
        return unavailable or (self.parent is not None and self.parent.exhausted())


_TICK_BUDGET = ContextVar('fts_upgrade_tick_budget', default=None)


def current_tick_budget():
    budget = _TICK_BUDGET.get()
    # fork后的新执行域沿自己的原维护入口计预算，不使用父进程锁或计数。
    return budget if budget is None or budget.pid == os.getpid() else None


@contextmanager
def fts_upgrade_tick_scope(*, deadline: float, max_steps: int = FTS_UPGRADE_TICK_MAX_STEPS_COUNT):
    if not math.isfinite(deadline) or type(max_steps) is not int or max_steps <= 0:
        raise ValueError('invalid FTS tick budget')
    parent = current_tick_budget()
    if parent is not None:
        deadline = min(deadline, parent.deadline)
    budget = FtsUpgradeTickBudget(deadline, max_steps, parent)
    token = _TICK_BUDGET.set(budget)
    try:
        yield budget
    finally:
        if budget.pid == os.getpid():
            with budget.lock:
                budget.closed = True
        else:
            budget.closed = True
        _TICK_BUDGET.reset(token)


_FIXED = ('data/local_store.sqlite3', 'data/local_store/local.db', 'local_store/local.db')
_WORKSPACES = ('workspace', 'runtime', 'workspaces')


# 热提示只保留有限owner；淘汰不丢责任，冷目录发现和库内job仍是恢复来源。
_FTS_HINT_OWNER_MAX_COUNT = 64
# 同owner热路径的提示数量上限；不持有Store、连接、正文或授权callback。
_FTS_HINT_STORE_MAX_COUNT = 8
# 拒绝把异常长配置路径变成常驻提示；不影响原Store路径合同。
_FTS_HINT_PATH_MAX_CHARS = 4096
_HOT_HINTS = (os.getpid(), Lock(), OrderedDict())


def _hot_hint_state():
    global _HOT_HINTS
    state = _HOT_HINTS
    if state[0] != os.getpid():
        # fork只继承字符串建议，不借父进程锁或把旧建议当当前活跃来源。
        state = (os.getpid(), Lock(), OrderedDict())
        _HOT_HINTS = state
    return state


def note_active_store(db_path):
    """纯进程提示；仅原规范路径，不检查文件、不打开SQLite、不授权维护。"""
    from .maintenance import _FTS_BATCH_LIMITS

    if _FTS_BATCH_LIMITS.get() is not None:
        return
    raw = os.fspath(db_path)
    if not isinstance(raw, str) or len(raw) > _FTS_HINT_PATH_MAX_CHARS:
        return
    path = Path(os.path.abspath(raw))
    if len(str(path)) > _FTS_HINT_PATH_MAX_CHARS or '\x00' in raw:
        return
    relative = None
    if (len(path.parts) > 6 and path.parts[-6:-3] == _WORKSPACES
            and path.parts[-2:] == ('local_store', 'local.db')):
        home = path.parents[5]
        relative = path.relative_to(home).as_posix()
    else:
        for fixed in _FIXED:
            parts = Path(fixed).parts
            if path.parts[-len(parts):] == parts and len(path.parts) > len(parts):
                relative, home = fixed, path.parents[len(parts) - 1]
                break
    if relative is None:
        return
    _, lock, owners = _hot_hint_state()
    with lock:
        stores = owners.get(str(home))
        if stores is None:
            stores = OrderedDict()
            owners[str(home)] = stores
        stores[relative] = None
        stores.move_to_end(relative)
        owners.move_to_end(str(home))
        while len(stores) > _FTS_HINT_STORE_MAX_COUNT:
            stores.popitem(last=False)
        while len(owners) > _FTS_HINT_OWNER_MAX_COUNT:
            owners.popitem(last=False)


def _take_active_store(home, pending):
    _, lock, owners = _hot_hint_state()
    key = os.path.abspath(os.fspath(home))
    with lock:
        stores = owners.get(key)
        if stores is None:
            return None
        while stores:
            relative, _ = stores.popitem(last=True)
            if relative not in pending:
                return relative
        owners.pop(key, None)
    return None


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


def _candidate(home, cursor, *, selected_only=False):
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
    if selected_only:
        return None
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


@sqlite_connection_scope(enabled=False)
def _advance_upgrade_slice(home, previous, now, *, commit=None, deadline=None, resume_only=False):
    """一拍一个原job；独立短连接不借用或改动业务缓存连接的等待策略。"""
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

    deadline = min(time.monotonic() + STORAGE_LANE_SLICE_MS / 1000,
                   deadline if deadline is not None else float("inf"))
    due = next_upgrade_at(home, previous, now)
    if due is None or (not resume_only and now < due):
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
    hot_next = cursor.get('hot_next', True)
    if type(hot_next) is not bool:
        raise ValueError('invalid FTS hot discovery turn')
    cursor.pop('selected', None)
    if resume_only and not pending:
        return None
    if pending and (resume_only or not discover):
        path = _candidate(home, {'selected': pending.pop(0)})
        cursor['discover_next'] = True
    else:
        try:
            hinted = _take_active_store(home, pending) if hot_next and len(pending) < 8 else None
        except Exception:  # noqa: BLE001 可丢提示失败只回到原冷发现，不吞扫描或原提交异常。
            hinted = None
        if hinted is not None:
            path = _candidate(home, {'selected': hinted}, selected_only=True)
            cursor['hot_next'] = False
        else:
            path = _candidate(home, cursor)
            cursor['hot_next'] = True
        cursor['discover_next'] = False
    cursor['pending'] = pending
    report = {'cursor': cursor, 'next_at': now + FTS_UPGRADE_INTERVAL_SECONDS,
              'scope': 'selected_store' if path is not None else 'discovery_page',
              'status': 'discovery', 'complete': False, 'progressed': False}
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
            if job is not None and job['phase'] == 'complete':
                # 旧FTS表已DROP仍可能有manifest/delta/map；只查责任存在性，不能误签整代清完。
                retired = retired or any(conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is not None
                    for table in ('maintenance_fts_docs', 'maintenance_fts_delta', 'maintenance_fts_retired_map'))
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
                if not store.fts_available:
                    cursor.pop('selected', None)
                    return {**report, 'status': 'unavailable'}
                try:
                    remaining_ms = max(0, (deadline - time.monotonic()) * 1000)
                    if remaining_ms <= 0:
                        return {**report, 'status': 'budget_exhausted'}
                    if needed:
                        result = store.rebuild_fts_step(max_rows=STORAGE_LANE_SLICE_ENTRIES,
                            max_bytes=STORAGE_LANE_SLICE_BYTES, max_ms=remaining_ms,
                            start_new=job is None or job['phase'] == 'complete')
                        report.update(status=result['status'], copied=result['copied'], progressed=result['progressed'])
                    else:
                        result = store.cleanup_fts_retired_step(max_rows=STORAGE_LANE_SLICE_ENTRIES,
                            max_bytes=STORAGE_LANE_SLICE_BYTES, max_ms=remaining_ms)
                        report.update(status='retiring' if result['pending'] else 'current', complete=not result['pending'],
                                      progressed=bool(result['removed']) or not result['pending'])
                        if report['complete']:
                            cursor.pop('selected', None)
                except _FtsByteBudget as exc:
                    remaining_ms = max(0, (deadline - time.monotonic()) * 1000)
                    if exc.required_bytes <= _FTS_SYNC_SINGLE_MAX_BYTES and remaining_ms > 0:
                        # 复用同步驱动原单文档准入，但不重置整拍deadline、不循环整库。
                        if needed:
                            result = store.rebuild_fts_step(max_rows=1, max_bytes=exc.required_bytes,
                                max_ms=remaining_ms, start_new=False)
                            report.update(status=result['status'], copied=result['copied'], progressed=result['progressed'],
                                          single_document_bytes=exc.required_bytes)
                        else:
                            result = store.cleanup_fts_retired_step(max_rows=1, max_bytes=exc.required_bytes,
                                max_ms=remaining_ms)
                            report.update(status='retiring' if result['pending'] else 'current',
                                          complete=not result['pending'], progressed=bool(result['removed']) or not result['pending'],
                                          single_document_bytes=exc.required_bytes)
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


@sqlite_connection_scope(enabled=False)
def advance_upgrade(home, previous, now, *, commit=None):
    """原调度一拍内有限追赶；只续已核owner路径，每个SQLite/业务锁在单片末释放。"""
    due = next_upgrade_at(home, previous, now)
    if due is None or now < due:
        return None
    budget = current_tick_budget()
    deadline = min(time.monotonic() + FTS_UPGRADE_CATCHUP_MAX_MS / 1000,
                   budget.deadline if budget is not None else float('inf'))
    working, report, attempts, progressed, single_bytes = previous, None, 0, False, 0
    for step in range(FTS_UPGRADE_CATCHUP_MAX_STEPS_COUNT):
        if time.monotonic() >= deadline or (budget is not None and not budget.reserve()):
            break
        attempts += 1
        result = _advance_upgrade_slice(home, working, now, commit=commit,
                                        deadline=deadline, resume_only=step > 0)
        if result is None:
            break
        report = result
        progressed = progressed or result['progressed']
        single_bytes = max(single_bytes, result.get('single_document_bytes', 0))
        working = {**previous, 'local_store_fts_upgrade': result}
        pending = result['cursor'].get('pending', [])
        if not pending:
            break
        if result['progressed'] or (result['status'] == 'current' and result['complete']):
            continue
        # 初次有界发现没选中库时，仍给既有pending一次机会；之后零进展立即让出。
        if step == 0 and result['status'] == 'discovery':
            continue
        break
    if report is None:
        # 不刷新未获资源owner的到期时刻：保留原优先续作顺序，hint由controller统一节流。
        return None
    report['cursor']['discover_next'] = True
    report['catchup'] = {'steps': attempts, 'progressed': progressed,
                         'single_document_max_bytes': single_bytes}
    return report
