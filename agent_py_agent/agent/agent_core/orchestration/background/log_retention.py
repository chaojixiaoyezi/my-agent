"""派工日志随实际进程退出留存；未知执行体和未收口业务证据永不按年龄删除。"""
from __future__ import annotations

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


class _DispatchScan:
    def __init__(self, descriptor):
        self.identity = _directory_identity(descriptor)
        self.entries = os.scandir(descriptor)
        self.days = []
        self.day_scan = None

    def close(self):
        if self.entries is not None:
            self.entries.close()
        if self.day_scan is not None:
            self.day_scan.close()


def _retire_directory(folder, descriptor, scan, result, *, owner_home, cutoff, max_entries, deadline, collect_days=False):
    while result['scanned'] < max_entries and time.monotonic() < deadline:
        entry = next(scan.entries, None)
        if entry is None:
            scan.entries.close()
            scan.entries = None
            return True
        result['scanned'] += 1
        if collect_days and _DAY_SHARD.fullmatch(entry.name):
            try:
                info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                scan.days.append(entry.name)
        elif entry.name.endswith('.exit.json'):
            result['deleted'] += _retire_exit_marker(folder, descriptor, entry.name, owner_home, cutoff)
    return False


def _retire_day(folder, descriptor, scan, result, *, owner_home, cutoff, cutoff_day, max_entries, deadline):
    day = scan.days[-1]
    try:
        child = os.open(day, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
    except OSError:
        if scan.day_scan is not None:
            scan.day_scan.close()
            scan.day_scan = None
        return True
    try:
        if scan.day_scan is None or scan.day_scan.identity != _directory_identity(child):
            if scan.day_scan is not None:
                scan.day_scan.close()
            scan.day_scan = _DispatchScan(child)
        if not _retire_directory(folder / day, child, scan.day_scan, result, owner_home=owner_home,
                                 cutoff=cutoff, max_entries=max_entries, deadline=deadline):
            return False
        scan.day_scan = None
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
    """先扫旧平铺日志，再按 UTC 日由旧到新。每片沿原目录身份续扫。"""
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
            with _LOCK:
                scan = _SCANS.pop(key, None)
            if scan is not None and scan.identity != _directory_identity(descriptor):
                scan.close()
                scan = None
            if scan is None:
                scan = _DispatchScan(descriptor)
            try:
                if scan.entries is not None:
                    if not _retire_directory(folder, descriptor, scan, result, owner_home=owner_home,
                                             cutoff=cutoff, max_entries=max_entries, deadline=deadline,
                                             collect_days=True):
                        return result
                    scan.days.sort(reverse=True)
                while scan.days and result['scanned'] < max_entries and time.monotonic() < deadline:
                    if not _retire_day(folder, descriptor, scan, result, owner_home=owner_home, cutoff=cutoff,
                                       cutoff_day=cutoff_day, max_entries=max_entries, deadline=deadline):
                        return result
                    scan.days.pop()
                result['complete'] = not scan.days
                return result
            finally:
                if result['complete']:
                    scan.close()
                else:
                    with _LOCK:
                        _SCANS[key] = scan
                        while len(_SCANS) > 32:
                            _SCANS.popitem(last=False)[1].close()
        finally:
            os.close(descriptor)
    finally:
        handle.lock.release()
