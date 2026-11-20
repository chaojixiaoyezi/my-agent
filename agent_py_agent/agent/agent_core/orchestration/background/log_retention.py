"""派工日志随实际进程退出留存；未知执行体和未收口业务证据永不按年龄删除。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

from ....common import json_io
from ....common.nofollow_fs import open_directory_beneath, read_bytes_beneath
from ....memory_store.retention_scan_budget import (
    RETENTION_SCAN_SLICE_ENTRIES,
    RETENTION_SCAN_SLICE_MS,
)
from ....user_space.directory_paging import directory_page

_DAY_SHARD = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_PROGRESS_NAME = '.retention_progress.json'
_PROGRESS_SCHEMA = 'background_dispatch_log_retention_progress.v2'
_MARKER_SUFFIX = '.exit.json'


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


def _marker_names(value):
    return type(value) is list and all(
        type(name) is str and name.endswith(_MARKER_SUFFIX) and '/' not in name and '\\' not in name
        for name in value)


def _fresh_progress(identity):
    """一轮从旧平铺层开始：phase=flat 分页根目录，phase=days 按日名升序分页各日目录。"""
    return {'schema': _PROGRESS_SCHEMA, 'identity': list(identity), 'phase': 'flat', 'day': '',
            'page': {}, 'listed': False, 'names': [], 'days': []}


# LLM: 进度记录是唯一续点（阶段、当前日、目录分页游标、本页未处理回执与旧平铺层收集到的日名）；
#   形状不符、根目录身份变化或旧版记录一律从头开始一轮，不沿可疑游标跳过任何回执。
# 函数用途: 读取并校验根目录下的派工日志留存续点。
def _load_progress(folder, identity):
    try:
        raw = read_bytes_beneath(folder, (_PROGRESS_NAME,), max_bytes=65536, require_dir_fd=True)
        row = json.loads(raw) if raw is not None else None
    except (OSError, ValueError):
        row = None
    if (isinstance(row, dict) and row.get('schema') == _PROGRESS_SCHEMA and row.get('identity') == list(identity)
            and row.get('phase') in ('flat', 'days') and type(row.get('day')) is str
            and (row['day'] == '' or (row['phase'] == 'days' and _DAY_SHARD.fullmatch(row['day'])))
            and isinstance(row.get('page'), dict) and type(row.get('listed')) is bool
            and _marker_names(row.get('names')) and type(row.get('days')) is list
            and all(type(day) is str and _DAY_SHARD.fullmatch(day) for day in row['days'])):
        return row
    return _fresh_progress(identity)


def _store_progress(folder, progress, complete):
    path = folder / _PROGRESS_NAME
    try:
        if complete:
            path.unlink(missing_ok=True)
        else:
            # 进度只是加速事实：丢失只会让下一轮从头复扫，不影响任何退役判定，故不 fsync。
            json_io.write_private_json_file_atomic_unlocked(path, progress, durable=False)
    except OSError:
        pass


def _list_page(folder, progress, limit):
    """沿持久游标读取当前目录一页；续点失效时只让本目录从头重列，不跳过任何回执。"""
    parts = (folder.name,) if progress['phase'] == 'flat' else (folder.name, progress['day'])
    try:
        return directory_page(folder.parent, parts, progress['page'], limit)
    except (OSError, ValueError):
        if not progress['page']:
            raise
        progress['page'] = {}
        return directory_page(folder.parent, parts, {}, limit)


def _retire_old_day(descriptor, progress, cutoff_day):
    # 保留仍可能接收新启动日志的近期目录。旧目录仅在仍是刚列完的原 inode 且为空时退役。
    day = progress['day']
    if day < cutoff_day:
        try:
            info = os.stat(day, dir_fd=descriptor, follow_symlinks=False)
            if [info.st_dev, info.st_ino] == progress['page'].get('directory_identity'):
                os.rmdir(day, dir_fd=descriptor)
        except OSError:
            pass


# LLM: 物理枚举只经共享 directory_page 有界分页（每页不超过剩余条目预算），根目录与每个日目录的游标每片落盘；
#   每片至少推进一个单元（一页、一个回执或一次换日），30 天截止、身份与退出证明核验、活跃日志租约不变。
# 函数用途: 先扫旧平铺日志，再按 UTC 日由旧到新，沿持久分页续点退役已收口的过期派工日志。
def retire_dispatch_logs_slice(folder, *, owner_home=None, now=None, max_entries=None, max_ms=None):
    folder = Path(folder).absolute()
    if owner_home is not None:
        owner_home = Path(owner_home)
    result = {'scanned': 0, 'deleted': 0, 'complete': False}
    if json_io.fcntl is None or not folder.exists():
        return {**result, 'complete': True}
    max_entries = RETENTION_SCAN_SLICE_ENTRIES if max_entries is None else max_entries
    max_ms = RETENTION_SCAN_SLICE_MS if max_ms is None else max_ms
    cutoff = (time.time() if now is None else now) - 30 * 86400
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
    deadline = time.monotonic() + max_ms / 1000
    handle = json_io.path_thread_lock(folder)
    if not handle.lock.acquire(blocking=False):
        return result
    try:
        try:
            descriptor = open_directory_beneath(folder.parent, (folder.name,), require_dir_fd=True)
        except FileNotFoundError:
            return {**result, 'complete': True}
        day_fd = None
        try:
            progress = _load_progress(folder, _directory_identity(descriptor))
            progressed = False
            while True:
                if progress['names']:
                    if progressed and time.monotonic() >= deadline:
                        break
                    progressed = True
                    if progress['phase'] == 'flat':
                        directory, fd = folder, descriptor
                    else:
                        if day_fd is None:
                            try:
                                day_fd = os.open(progress['day'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                 dir_fd=descriptor)
                            except OSError:
                                progress['names'] = []  # 日目录在列名后消失或被替换：放弃该日剩余名单。
                                continue
                        directory, fd = folder / progress['day'], day_fd
                    result['deleted'] += _retire_exit_marker(directory, fd, progress['names'].pop(0), owner_home, cutoff)
                    continue
                if progressed and (result['scanned'] >= max_entries or time.monotonic() >= deadline):
                    break
                if not progress['listed']:
                    try:
                        names, progress['page'], progress['listed'] = _list_page(
                            folder, progress, max(1, max_entries - result['scanned']))
                    except OSError:
                        if progress['phase'] == 'flat':
                            raise
                        names, progress['page'], progress['listed'] = [], {}, True  # 日目录不可读：跳过该日。
                    result['scanned'] += len(names)
                    progressed = True
                    if progress['phase'] == 'flat':
                        progress['days'].extend(name for name in names
                                                if _DAY_SHARD.fullmatch(name) and name not in progress['days'])
                    progress['names'] = [name for name in names if name.endswith(_MARKER_SUFFIX)]
                    continue
                # 当前目录已列完且其回执均已处理。
                if progress['phase'] == 'flat':
                    progress.update(phase='days', day='', days=sorted(progress['days']))
                    continue
                if progress['day']:
                    if day_fd is not None:
                        os.close(day_fd)
                        day_fd = None
                    _retire_old_day(descriptor, progress, cutoff_day)
                    progress['day'] = ''
                    continue
                if not progress['days']:
                    result['complete'] = True
                    break
                # 换日计入预算：大量空旧目录也按片推进。
                result['scanned'] += 1
                progressed = True
                progress.update(day=progress['days'].pop(0), page={}, listed=False)
            _store_progress(folder, progress, result['complete'])
            return result
        finally:
            if day_fd is not None:
                os.close(day_fd)
            os.close(descriptor)
    finally:
        handle.lock.release()
