"""临时磁盘投影保留分片续点；只有稳定且完整的 canonical 核对才能证明空闲。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time

from ..io.bounded_directory import DirectoryCursor
from ..user_space.directory_paging import directory_page
from .store_claims import _read_claim_report

_LOCK = threading.RLock()
_CACHE = None
_PORTABLE = {}
_NATIVE = os.name != "nt"


def _signature(path):
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


def _before_fork():
    # 动态读取当前锁：child 重置后再次 fork 也必须守住自己的 SQL/目录流边界。
    _LOCK.acquire()


def _after_fork_parent():
    _LOCK.release()


def _after_fork():
    global _LOCK, _CACHE
    _LOCK = threading.RLock()
    if _CACHE is not None:
        # child 不拥有父进程临时目录：先解绑继承的 finalizer，再丢弃连接副本。
        _CACHE[1]._finalizer.detach()
        _CACHE[2].close()
        _CACHE = None
    for stream, _ in _PORTABLE.values():
        stream.close()
    _PORTABLE.clear()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_parent,
                        after_in_child=_after_fork)


def _connection():
    global _CACHE
    if _CACHE is None or _CACHE[0] != os.getpid():
        # 一个进程只持有一个缓存连接，不随 owner 数量保留目录 FD 或 Python 活跃集。
        directory = tempfile.TemporaryDirectory(prefix='my-agent-claim-view-')
        conn = sqlite3.connect(os.path.join(directory.name, 'projection.sqlite3'),
                               check_same_thread=False, timeout=0)
        conn.execute('PRAGMA journal_mode=MEMORY')
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA cache_size=-256')
        conn.execute('PRAGMA mmap_size=0')
        conn.executescript('''
            CREATE TABLE scans (store TEXT PRIMARY KEY, signature TEXT, generation INTEGER,
                cursor TEXT, complete INTEGER, unknown INTEGER, checked REAL, last_access REAL);
            CREATE INDEX scan_access ON scans(last_access);
            CREATE INDEX scan_waiting ON scans(complete,generation);
            CREATE TABLE busy (store TEXT, generation INTEGER, thread TEXT,
                PRIMARY KEY(store,generation,thread));
            CREATE TABLE retired (store TEXT, generation INTEGER, PRIMARY KEY(store,generation));
        ''')
        _CACHE = os.getpid(), directory, conn
    return _CACHE[2]


def _page(conn, key, directory, cursor):
    if _NATIVE:
        return directory_page(directory.parent, (directory.name,), cursor, 1)
    # Windows 没有可恢复的 dir_fd cookie：固定槽只接纳队首，不淘汰进行中的流。
    for stored, (stream, accessed) in list(_PORTABLE.items()):
        if stored != key and time.monotonic() - accessed > 60:
            stream.close()
            del _PORTABLE[stored]
            conn.execute('INSERT OR IGNORE INTO retired SELECT store,generation FROM scans WHERE store=?', (stored,))
            conn.execute('DELETE FROM scans WHERE store=?', (stored,))
    if key not in _PORTABLE:
        oldest = conn.execute('SELECT store FROM scans WHERE complete=0 '
                              'ORDER BY generation LIMIT 1').fetchone()
        if len(_PORTABLE) >= 8 or oldest and oldest[0] != key and oldest[0] not in _PORTABLE:
            return [], cursor, False
        _PORTABLE[key] = DirectoryCursor(), time.monotonic()
    stream, _ = _PORTABLE[key]
    _PORTABLE[key] = stream, time.monotonic()
    entry = stream.next_entry(directory)
    if entry is None:
        del _PORTABLE[key]
        return [], {}, True
    return [entry.name], {}, False


def _cleanup(conn):
    # 只过期长期未访问的 store；数量压力不会丢失活跃扫描续点。
    for key, generation in conn.execute('SELECT store,generation FROM scans '
                                       'WHERE last_access<? ORDER BY last_access LIMIT 4',
                                       (time.monotonic() - 3600,)).fetchall():
        conn.execute('INSERT OR IGNORE INTO retired VALUES (?,?)', (key, generation))
        conn.execute('DELETE FROM scans WHERE store=?', (key,))
    retired = conn.execute('SELECT store,generation FROM retired LIMIT 1').fetchone()
    if retired is None:
        return
    conn.execute('DELETE FROM busy WHERE rowid IN (SELECT rowid FROM busy '
                 'WHERE store=? AND generation=? LIMIT 128)', retired)
    if conn.execute('SELECT 1 FROM busy WHERE store=? AND generation=? LIMIT 1', retired).fetchone() is None:
        conn.execute('DELETE FROM retired WHERE store=? AND generation=?', retired)


def claim_activity_state(storage, thread_id):
    """每拍最多128项/25ms；跨 store 淘汰、线程切换不丢失目录续点。

    目录 cookie 与活跃线程只存在进程私有临时 SQLite 中，不授予 claim 状态。
    canonical 原子换叶使目录版本变化，旧代投影立即失效、垃圾分片回收。
    """
    directory = storage.background_claims_dir
    try:
        with _LOCK:
            conn = _connection()
            with conn:
                _cleanup(conn)
                signature = json.dumps(_signature(directory))
                key = str(directory.absolute())
                row = conn.execute('SELECT signature,generation,cursor,complete,unknown,checked '
                                   'FROM scans WHERE store=?', (key,)).fetchone()
                if row is None:
                    generation, cursor, complete, unknown, checked = time.monotonic_ns(), {}, False, False, 0
                elif (row[0] != signature or row[3] and time.monotonic() - row[5] >= 5
                      or key in _PORTABLE and time.monotonic() - _PORTABLE[key][1] > 60):
                    conn.execute('INSERT OR IGNORE INTO retired VALUES (?,?)', (key, row[1]))
                    portable = _PORTABLE.pop(key, None)
                    if portable:
                        portable[0].close()
                    generation, cursor, complete, unknown, checked = time.monotonic_ns(), {}, False, False, 0
                else:
                    _, generation, encoded, complete, unknown, checked = row
                    cursor = json.loads(encoded)
                conn.execute('INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?,?,?)',
                             (key, signature, generation, json.dumps(cursor), int(complete), int(unknown), checked, time.monotonic()))
                deadline = time.monotonic() + .025
                if not complete:
                    # 单项推进保证 canonical 读取也受时间预算约束；cookie 立即落入本事务。
                    for _ in range(128):
                        names, cursor, complete = _page(conn, key, directory, cursor)
                        for name in names:
                            if not name.endswith('.json'):
                                continue
                            path = directory / name
                            if path.is_symlink() or not path.is_file():
                                unknown = True
                                continue
                            claim, error = _read_claim_report(path)
                            if error or claim.get('status') not in {'running', 'claimed', 'finished', 'failed', 'cancelled'}:
                                unknown = True
                            elif claim['status'] in {'running', 'claimed'}:
                                identity = claim.get('thread_id')
                                if isinstance(identity, str) and identity:
                                    conn.execute('INSERT OR IGNORE INTO busy VALUES (?,?,?)', (key, generation, identity))
                                else:
                                    unknown = True
                        if complete or time.monotonic() >= deadline:
                            break
                    if complete:
                        checked = time.monotonic()
                conn.execute('INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?,?,?)',
                             (key, signature, generation, json.dumps(cursor), int(complete), int(unknown), checked, time.monotonic()))
                if json.dumps(_signature(directory)) != signature:
                    return 'activity_unknown'
                if conn.execute('SELECT 1 FROM busy WHERE store=? AND generation=? AND thread=?',
                                (key, generation, thread_id)).fetchone():
                    return 'foreground_active'
                if not complete or unknown:
                    return 'activity_unknown'
                return ''
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return 'activity_unknown'
