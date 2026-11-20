"""派工日志随实际进程退出留存；未知执行体和未收口业务证据永不按年龄删除。"""
from __future__ import annotations

import bisect
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from ....common import json_io
from ....common.nofollow_fs import open_directory_beneath, read_bytes_beneath

_SCANS = OrderedDict()
_LOCK = threading.RLock()
_DAY_SHARD = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_PROGRESS_NAME = '.retention_progress.json'
_PROGRESS_SCHEMA = 'background_dispatch_log_retention_progress.v1'


def pin_dispatch_log(handle):
    """SH随stdout同一open-file-description继承，孙进程未关闭输出也阻止清理。"""
    if json_io.fcntl is not None:
        json_io.fcntl.flock(handle.fileno(), json_io.fcntl.LOCK_SH)


def record_dispatch_log_exit(path, *, returncode, run_ids, owner_home, attempt_ids=None, expected_identity=None, capture_expected=False):
    if (type(returncode) is not int or not run_ids or type(attempt_ids) is not dict
            or set(attempt_ids) != set(run_ids) or any(type(value) is not str or not value for value in attempt_ids.values())
            or not isinstance(expected_identity, (list, tuple)) or len(expected_identity) != 2):
        return
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != tuple(expected_identity):
        return
    if capture_expected:
        from ....io.bounded_process_log import read_capture_report
        capture = read_capture_report(path, expected_identity)
        if capture is None or not capture[0]['eof'] or capture[0]['error_types']:
            return  # 未完整排空的日志保持恢复证据，不把进程wait当输出EOF。
    json_io.write_private_json_file_atomic_unlocked(path.with_suffix('.exit.json'), {
        'schema': 'background_dispatch_log_exit.v2', 'returncode': returncode,
        'run_ids': list(run_ids), 'attempt_ids': dict(attempt_ids), 'owner_home': str(owner_home), 'finished_at': time.time(),
        'identity': [info.st_dev, info.st_ino], 'capture_expected': bool(capture_expected),
    })


def _business_closed(owner, run_ids, attempt_ids):
    from ....io.sqlite_probe import require_plain_sqlite_files, sqlite_readonly_probe_uri
    from ....runtime_db.current_obligations import closed_attempt_bindings, recorded_run_ids
    from ....runtime_db.schema import runtime_db_path

    if (not run_ids or len(run_ids) > 512 or not owner.is_absolute() or type(attempt_ids) is not dict
            or set(attempt_ids) != set(run_ids)):
        return False
    if any(type(run_id) is not str or not run_id or '/' in run_id or '\\' in run_id for run_id in run_ids):
        return False
    policy_raw = read_bytes_beneath(owner, ('retention.json',), max_bytes=1048576, require_dir_fd=True)
    policy = json.loads(policy_raw) if policy_raw is not None else {}
    if not isinstance(policy, dict) or policy.get('legal_hold') is True:
        return False
    protected_tasks = policy.get('legal_hold_task_ids', [])
    if not isinstance(protected_tasks, list):
        return False
    observed = False
    # 每个库只按原launch准确attempt主键查一次；无代次的旧日志不能扫描全历史猜归属。
    for db in (runtime_db_path(owner), owner / 'data/runtime.db'):
        try:
            require_plain_sqlite_files(db, beneath=owner)
        except FileNotFoundError:
            continue
        with closing(sqlite3.connect(sqlite_readonly_probe_uri(db), uri=True, timeout=0)) as conn:
            conn.execute('BEGIN')
            if not recorded_run_ids(conn, run_ids):
                continue
            closed, task_ids = closed_attempt_bindings(conn, attempt_ids)
            if not closed or task_ids & set(protected_tasks):
                return False
            observed = True
    return observed


def _retire_exit_marker(folder, descriptor, marker, owner_home, cutoff):
    try:
        raw = read_bytes_beneath(folder, (marker,), max_bytes=1048576, require_dir_fd=True)
        row = json.loads(raw)
        if not isinstance(row, dict):
            return False
        ended = row.get('finished_at')
        owner = Path(row.get('owner_home', ''))
        if (row.get('schema') != 'background_dispatch_log_exit.v2' or type(ended) not in (int, float)
                or not 0 < ended < cutoff or type(row.get('returncode')) is not int
                or owner_home is not None and owner != owner_home
                or not _business_closed(owner, row.get('run_ids', []), row.get('attempt_ids'))):
            return False
        from ....io.bounded_process_log import capture_path, read_capture_report
        name = marker[:-len('.exit.json')] + '.log'
        capture = read_capture_report(folder / name, row.get('identity', []))
        if row.get('capture_expected'):
            if capture is None or not capture[0]['eof'] or capture[0]['error_types']:
                return False
        elif capture is not None:
            return False
        try:
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        except FileNotFoundError:
            # 手工删日志或上轮只删掉日志时，已有退出与业务终态证明仍允许退役原回执。
            if read_bytes_beneath(folder, (marker,), max_bytes=1048576, require_dir_fd=True) == raw:
                os.unlink(marker, dir_fd=descriptor)
                if capture is not None:
                    os.unlink(capture_path(folder / name).name, dir_fd=descriptor)
            return False
        try:
            handle = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise
        with handle as log:
            json_io.fcntl.flock(log.fileno(), json_io.fcntl.LOCK_EX | json_io.fcntl.LOCK_NB)
            info = os.fstat(log.fileno())
            latest = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or [info.st_dev, info.st_ino] != row.get('identity')
                    or not os.path.samestat(info, latest)
                    or info.st_mtime > ended
                    or read_bytes_beneath(folder, (marker,), max_bytes=1048576, require_dir_fd=True) != raw):
                return False
            if capture is not None:
                current_capture = read_capture_report(folder / name, row['identity'])
                if current_capture is None or current_capture[1] != capture[1]:
                    return False
            os.unlink(name, dir_fd=descriptor)
            os.unlink(marker, dir_fd=descriptor)
            if capture is not None:
                os.unlink(capture_path(folder / name).name, dir_fd=descriptor)
            return True
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def _directory_identity(descriptor):
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


class _ScanProgress:
    """单根续扫位置：按名有序游标（day, after），不持有任何目录句柄。

    day 为空串表示旧平铺层；after 是该层最后处理过的回执名。名单只是可重建的读取侧缓存，
    缓存淘汰或进程重启后从同目录的轻量进度文件恢复游标，重新列目录并沿原位置向前推进。
    """

    __slots__ = ('identity', 'day', 'after', 'days', 'names', 'names_identity')

    def __init__(self, identity, day='', after=''):
        self.identity = identity
        self.day = day
        self.after = after
        self.days = None
        self.names = None
        self.names_identity = None


def _load_progress(folder, identity):
    try:
        raw = read_bytes_beneath(folder, (_PROGRESS_NAME,), max_bytes=65536, require_dir_fd=True)
        row = json.loads(raw) if raw is not None else None
    except (OSError, ValueError):
        row = None
    if (isinstance(row, dict) and row.get('schema') == _PROGRESS_SCHEMA and row.get('identity') == list(identity)
            and type(row.get('day')) is str and type(row.get('after')) is str
            and (row['day'] == '' or _DAY_SHARD.fullmatch(row['day']))):
        return _ScanProgress(identity, row['day'], row['after'])
    return _ScanProgress(identity)


def _store_progress(folder, progress, complete):
    path = folder / _PROGRESS_NAME
    try:
        if complete:
            path.unlink(missing_ok=True)
        else:
            # 进度只是加速事实：丢失只会让下一轮从头复扫，不影响任何退役判定，故不 fsync。
            json_io.write_private_json_file_atomic_unlocked(path, {
                'schema': _PROGRESS_SCHEMA, 'identity': list(progress.identity),
                'day': progress.day, 'after': progress.after,
            }, durable=False)
    except OSError:
        pass


def _list_top_level(descriptor):
    flat, days = [], []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if _DAY_SHARD.fullmatch(entry.name):
                days.append(entry.name)
            elif entry.name.endswith('.exit.json'):
                flat.append(entry.name)
    flat.sort(reverse=True)
    days.sort()
    return flat, days


def _list_markers(descriptor):
    with os.scandir(descriptor) as entries:
        return sorted((entry.name for entry in entries if entry.name.endswith('.exit.json')), reverse=True)


def _drain(folder, descriptor, progress, result, *, owner_home, cutoff, max_entries, deadline):
    """按名升序处理游标之后的回执；names 逆序存放以便 pop() 取最小名。"""
    while progress.names:
        if result['scanned'] >= max_entries or time.monotonic() >= deadline:
            return False
        name = progress.names.pop()
        result['scanned'] += 1
        result['deleted'] += _retire_exit_marker(folder, descriptor, name, owner_home, cutoff)
        progress.after = name
    return True


def _retire_day(folder, descriptor, progress, result, *, owner_home, cutoff, cutoff_day, max_entries, deadline):
    day = progress.day
    try:
        child = os.open(day, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
    except OSError:
        return True
    try:
        identity = _directory_identity(child)
        if progress.names is None or progress.names_identity != identity:
            progress.names = [name for name in _list_markers(child) if name > progress.after]
            progress.names_identity = identity
        if not _drain(folder / day, child, progress, result, owner_home=owner_home, cutoff=cutoff,
                      max_entries=max_entries, deadline=deadline):
            return False
        # 保留仍可能接收新启动日志的近期目录。旧目录仅在原 inode 且为空时退役。
        if day < cutoff_day:
            try:
                info = os.stat(day, dir_fd=descriptor, follow_symlinks=False)
                if os.path.samestat(info, os.fstat(child)):
                    os.rmdir(day, dir_fd=descriptor)
            except OSError:
                pass
        return True
    finally:
        os.close(child)


def retire_dispatch_logs_slice(folder, *, owner_home=None, now=None, max_entries=128, max_ms=25):
    """先扫旧平铺日志，再按 UTC 日由旧到新。每片沿持久名游标续扫，与句柄缓存无关。"""
    folder = Path(folder).absolute()
    if owner_home is not None:
        owner_home = Path(owner_home)
    result = {'scanned': 0, 'deleted': 0, 'complete': False}
    if json_io.fcntl is None or not folder.exists():
        return {**result, 'complete': True}
    cutoff = (time.time() if now is None else now) - 30 * 86400
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
    deadline = time.monotonic() + max_ms / 1000
    key = (folder, owner_home)
    handle = json_io.path_thread_lock(folder)
    if not handle.lock.acquire(blocking=False):
        return result
    try:
        try:
            descriptor = open_directory_beneath(folder.parent, (folder.name,), require_dir_fd=True)
        except FileNotFoundError:
            return {**result, 'complete': True}
        try:
            identity = _directory_identity(descriptor)
            with _LOCK:
                progress = _SCANS.pop(key, None)
            if progress is None or progress.identity != identity:
                progress = _load_progress(folder, identity)
            position = (progress.day, progress.after)
            options = {'owner_home': owner_home, 'cutoff': cutoff, 'max_entries': max_entries, 'deadline': deadline}
            while result['scanned'] < max_entries and time.monotonic() < deadline:
                if progress.day == '':
                    if progress.names is None or progress.names_identity != identity:
                        flat, progress.days = _list_top_level(descriptor)
                        progress.names = [name for name in flat if name > progress.after]
                        progress.names_identity = identity
                    if not _drain(folder, descriptor, progress, result, **options):
                        break
                elif not _retire_day(folder, descriptor, progress, result, cutoff_day=cutoff_day, **options):
                    break
                if progress.days is None:
                    progress.days = _list_top_level(descriptor)[1]
                following = progress.days[bisect.bisect_right(progress.days, progress.day):]
                if not following:
                    result['complete'] = True
                    break
                # 换日计入预算：大量空旧目录也按片推进，且每片至少前进一个回执或一个日目录。
                result['scanned'] += 1
                progress.day, progress.after, progress.names = following[0], '', None
            if result['complete'] or (progress.day, progress.after) != position:
                _store_progress(folder, progress, result['complete'])
            if not result['complete']:
                with _LOCK:
                    _SCANS[key] = progress
                    while len(_SCANS) > 32:
                        _SCANS.popitem(last=False)
            return result
        finally:
            os.close(descriptor)
    finally:
        handle.lock.release()
