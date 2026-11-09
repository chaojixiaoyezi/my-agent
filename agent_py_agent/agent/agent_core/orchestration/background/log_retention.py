"""派工日志随实际进程退出留存；未知执行体和未收口业务证据永不按年龄删除。"""
from __future__ import annotations

import json
import os
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


def pin_dispatch_log(handle):
    """SH随stdout同一open-file-description继承，孙进程未关闭输出也阻止清理。"""
    if json_io.fcntl is not None:
        json_io.fcntl.flock(handle.fileno(), json_io.fcntl.LOCK_SH)


def record_dispatch_log_exit(path, *, returncode, run_ids, owner_home):
    if type(returncode) is not int or not run_ids:
        return
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return
    json_io.write_private_json_file_atomic_unlocked(path.with_suffix('.exit.json'), {
        'schema': 'background_dispatch_log_exit.v1', 'returncode': returncode,
        'run_ids': list(run_ids), 'owner_home': str(owner_home), 'finished_at': time.time(),
        'identity': [info.st_dev, info.st_ino],
    })


def _business_closed(owner, run_ids):
    from ....io.sqlite_probe import require_plain_sqlite_files, sqlite_readonly_probe_uri
    from ....runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
    from ....runtime_db.schema import runtime_db_path

    if not run_ids or len(run_ids) > 512 or not owner.is_absolute():
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
    observed = set()
    marks = ','.join('?' for _ in run_ids)
    # 同一批次只开固定两个规范库；不为每个run重新打开SQLite或扫历史文件。
    for db in (runtime_db_path(owner), owner / 'data/runtime.db'):
        try:
            require_plain_sqlite_files(db, beneath=owner)
        except FileNotFoundError:
            continue
        with closing(sqlite3.connect(sqlite_readonly_probe_uri(db), uri=True, timeout=0)) as conn:
            conn.execute('BEGIN')
            rows = conn.execute(f'SELECT run_id,status,task_run_id FROM agent_runs WHERE run_id IN ({marks})', run_ids).fetchall()
            if any(row[1] not in AGENT_RUN_TERMINAL_STATUSES for row in rows):
                return False
            observed.update(row[0] for row in rows)
            if protected_tasks and conn.execute(f'SELECT 1 FROM task_runs WHERE task_run_id IN (SELECT task_run_id FROM agent_runs WHERE run_id IN ({marks})) AND task_id IN (' + ','.join('?' for _ in protected_tasks) + ') LIMIT 1', (*run_ids, *protected_tasks)).fetchone():
                return False
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wake_queue'").fetchone():
                if conn.execute(f"SELECT 1 FROM wake_queue WHERE status NOT IN ('consumed','cancelled','done') AND "
                    f"(root_run_id IN ({marks}) OR root_task_id IN "
                    f"(SELECT task_id FROM task_runs WHERE task_run_id IN "
                    f"(SELECT task_run_id FROM agent_runs WHERE run_id IN ({marks})))) LIMIT 1", (*run_ids, *run_ids)).fetchone():
                    return False
    return observed == set(run_ids)


def retire_dispatch_logs_slice(folder, *, owner_home=None, now=None, max_entries=128, max_ms=25):
    """至多一片目录项；默认30天，EX证明stdout所有后代FD已关闭，canonical证明业务已结。"""
    folder = Path(folder)
    result = {'scanned': 0, 'deleted': 0, 'complete': False}
    if json_io.fcntl is None or not folder.exists():
        return {**result, 'complete': True}
    current = time.time() if now is None else now
    deadline = time.monotonic() + max_ms / 1000
    descriptor = open_directory_beneath(folder.parent, (folder.name,), require_dir_fd=True)
    try:
        with _LOCK:
            key = str(folder.absolute())
            identity = os.fstat(descriptor)
            cached = _SCANS.pop(key, None)
            if cached and cached[0] != (identity.st_dev, identity.st_ino):
                cached[1].close()
                cached = None
            entries = cached[1] if cached else os.scandir(descriptor)
            try:
                while result['scanned'] < max_entries and time.monotonic() < deadline:
                    try:
                        entry = next(entries)
                    except StopIteration:
                        result['complete'] = True
                        break
                    result['scanned'] += 1
                    if not entry.name.endswith('.exit.json'):
                        continue
                    try:
                        raw = read_bytes_beneath(folder, (entry.name,), max_bytes=1048576, require_dir_fd=True)
                        row = json.loads(raw)
                        ended = row.get('finished_at')
                        owner = Path(row.get('owner_home', ''))
                        if (row.get('schema') != 'background_dispatch_log_exit.v1' or type(ended) not in (int, float)
                                or not 0 < ended < current - 30 * 86400 or type(row.get('returncode')) is not int
                                or owner_home is not None and owner != Path(owner_home)
                                or not _business_closed(owner, row.get('run_ids', []))):
                            continue
                        name = entry.name[:-len('.exit.json')] + '.log'
                        try:
                            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=descriptor)
                        except FileNotFoundError:
                            # 手工删日志或上轮只删掉日志时，已有退出与业务终态证明仍允许退役原回执。
                            if read_bytes_beneath(folder, (entry.name,), max_bytes=1048576, require_dir_fd=True) == raw:
                                os.unlink(entry.name, dir_fd=descriptor)
                            continue
                        with closing(os.fdopen(fd, 'rb')) as log:
                            json_io.fcntl.flock(log.fileno(), json_io.fcntl.LOCK_EX | json_io.fcntl.LOCK_NB)
                            info = os.fstat(log.fileno())
                            latest = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                    or [info.st_dev, info.st_ino] != row.get('identity')
                                    or not os.path.samestat(info, latest)
                                    or info.st_mtime > ended
                                    or read_bytes_beneath(folder, (entry.name,), max_bytes=1048576, require_dir_fd=True) != raw):
                                continue
                            os.unlink(name, dir_fd=descriptor)
                            os.unlink(entry.name, dir_fd=descriptor)
                            result['deleted'] += 1
                    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                        continue
            finally:
                if result['complete']:
                    entries.close()
                else:
                    _SCANS[key] = ((identity.st_dev, identity.st_ino), entries)
                    while len(_SCANS) > 32:
                        _SCANS.popitem(last=False)[1][1].close()
    finally:
        os.close(descriptor)
    return result
