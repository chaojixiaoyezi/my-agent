"""临时磁盘投影保留分片续点；只有稳定且完整的 canonical 核对才能证明空闲。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time

from ..common.nofollow_fs import _portable_directory, _supports_dir_fd, open_directory_beneath
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


def _small_proof(directory, deadline):
    """最多17项区分小目录；原子换叶的 inode 使同时间刻度的变更也可见。"""
    descriptor = None
    try:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return hashlib.sha256(b'[]').hexdigest(), 0, ()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('claim_directory_not_real')
        strict = (_supports_dir_fd() and getattr(os, 'O_NOFOLLOW', 0) and getattr(os, 'O_DIRECTORY', 0))
        if strict:
            descriptor = open_directory_beneath(directory.parent, (directory.name,), require_dir_fd=True)
        else:
            _portable_directory(directory.parent, (directory.name,), create=False)
        rows, bounded = [], []
        with os.scandir(directory if descriptor is None else descriptor) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise TimeoutError('claim_probe_budget')
                if len(bounded) == 16:
                    return None, 17, None
                bounded.append(entry)
            # 先确认确为小目录再stat；大历史目录不做每拍两组无用的16次元数据IO。
            for entry in bounded:
                if time.monotonic() >= deadline:
                    raise TimeoutError('claim_probe_budget')
                info = entry.stat(follow_symlinks=False)
                rows.append((entry.name, info.st_dev, info.st_ino, info.st_size,
                             info.st_mtime_ns, info.st_ctime_ns, info.st_mode))
        return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(), len(rows), tuple(row[0] for row in rows)
    finally:
        if descriptor is not None:
            os.close(descriptor)


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


def _consume_claim(conn, key, generation, directory, name):
    if not name.endswith('.json'):
        return False
    path = directory / name
    if path.is_symlink() or not path.is_file():
        return True
    claim, error = _read_claim_report(path)
    if error or claim.get('status') not in {'running', 'claimed', 'finished', 'failed', 'cancelled'}:
        return True
    if claim['status'] in {'running', 'claimed'}:
        identity = claim.get('thread_id')
        if not isinstance(identity, str) or not identity:
            return True
        conn.execute('INSERT OR IGNORE INTO busy VALUES (?,?,?)', (key, generation, identity))
    return False


def _advance_portable_resident(conn):
    # 只借已由原入口校验并持有的目录流，不替当前请求发现或打开其它owner根。
    # 当前调用仍只收到自己的结果；所有帮助工作共享本拍条目/时间预算，不写canonical。
    if not _PORTABLE:
        return
    keys = tuple(_PORTABLE)
    row = conn.execute('SELECT store,signature,generation,unknown FROM scans WHERE complete=0 AND store IN ('
                       + ','.join('?' for _ in keys) + ') ORDER BY generation LIMIT 1', keys).fetchone()
    if row is None:
        return
    key, signature, generation, unknown = row
    stream, _accessed = _PORTABLE[key]
    from pathlib import Path
    directory = Path(key)
    expected = json.loads(signature)
    def retire():
        stream.close()
        _PORTABLE.pop(key, None)
        conn.execute('INSERT OR IGNORE INTO retired VALUES (?,?)', (key, generation))
        conn.execute('DELETE FROM scans WHERE store=?', (key,))
    def same_directory():
        try:
            real = stat.S_ISDIR(directory.lstat().st_mode)
        except FileNotFoundError:
            return False
        return real and json.dumps(_signature(directory)) == signature
    if (stream.identity is None or expected is None or tuple(expected[:2]) != stream.identity
            or not same_directory()):
        retire()
        return
    complete = False
    try:
        # 不能调用会在换代时重新open的next_entry；只消费本来持有的同一目录流。
        entry = next(stream.entries)
    except StopIteration:
        complete = True
    if not same_directory():
        retire()
        return
    if not complete:
        unknown = _consume_claim(conn, key, generation, directory, entry.name) or bool(unknown)
    if not same_directory():
        retire()
        return
    if complete:
        stream.close()
        _PORTABLE.pop(key, None)
    else:
        _PORTABLE[key] = stream, time.monotonic()
    conn.execute('UPDATE scans SET complete=?,unknown=?,checked=? WHERE store=? AND generation=?',
                 (int(complete), int(unknown), time.monotonic() if complete else 0, key, generation))


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
        if len(_PORTABLE) >= 8 or (_PORTABLE and oldest and oldest[0] != key and oldest[0] not in _PORTABLE):
            _advance_portable_resident(conn)
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
            try:
                with conn:
                    _cleanup(conn)
                    deadline = time.monotonic() + .025
                    proof, probe_items, small_names = _small_proof(directory, deadline)
                    signature = json.dumps(_signature(directory))
                    key = str(directory.absolute())
                    row = conn.execute('SELECT signature,generation,cursor,complete,unknown,checked '
                                       'FROM scans WHERE store=?', (key,)).fetchone()
                    if row is None:
                        generation, cursor, complete, unknown, checked = time.monotonic_ns(), {}, False, False, 0
                    elif (row[0] != signature or json.loads(row[2]).get('_small_proof') != proof
                          or row[3] and time.monotonic() - row[5] >= 5
                          or json.loads(row[2]).get('_awaiting_proof') and time.monotonic() - json.loads(row[2]).get('_source_checked', 0) >= 5
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
                    complete = bool(complete or cursor.get('_awaiting_proof'))
                    if not complete:
                        # 单项推进保证 canonical 读取也受时间预算约束；cookie 立即落入本事务。
                        # 为前后小目录探针各预留至多17项，总目录项成本仍不超过128。
                        for _ in range(128 - probe_items - 17):
                            if small_names is not None:
                                # 小目录proof已有完整且有界的名字集，不应等待无关大目录的八个FD槽。
                                position = cursor.get('_small_position', 0)
                                names = small_names[position:position + 1]
                                cursor['_small_position'] = position + len(names)
                                complete = position + len(names) >= len(small_names)
                            else:
                                names, cursor, complete = _page(conn, key, directory, cursor)
                            for name in names:
                                unknown = _consume_claim(conn, key, generation, directory, name) or unknown
                            if complete or time.monotonic() >= deadline:
                                break
                        if complete:
                            checked = time.monotonic()
                    if complete and not checked:
                        checked = cursor.get('_source_checked', 0) or time.monotonic()
                    if complete:
                        cursor['_source_checked'] = checked
                    cursor['_awaiting_proof'] = bool(complete)
                    cursor['_small_proof'] = proof
                    conn.execute('INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?,?,?)',
                                 (key, signature, generation, json.dumps(cursor), int(complete), int(unknown), checked, time.monotonic()))
                    if time.monotonic() >= deadline:
                        # 正常预算yield必须提交本片续点/busy证明；让尾部proof抛Timeout会回滚SQL，
                        # 但Windows目录流已经前移，随后可能漏掉这一片的活动claim。
                        conn.execute('UPDATE scans SET complete=0,checked=0 WHERE store=?', (key,))
                        return 'activity_unknown'
                    try:
                        after_proof, _, _ = _small_proof(directory, deadline)
                    except TimeoutError:
                        conn.execute('UPDATE scans SET complete=0,checked=0 WHERE store=?', (key,))
                        return 'activity_unknown'
                    if after_proof != proof or json.dumps(_signature(directory)) != signature:
                        return 'activity_unknown'
                    if conn.execute('SELECT 1 FROM busy WHERE store=? AND generation=? AND thread=?',
                                    (key, generation, thread_id)).fetchone():
                        return 'foreground_active'
                    if not complete or unknown:
                        return 'activity_unknown'
                    return ''
            except (OSError, ValueError, TypeError, sqlite3.Error):
                # 回滚和便携流复位守同一模块锁，其他线程不能看到丢失busy证明的前移流。
                for stream, _accessed in _PORTABLE.values():
                    try:
                        stream.close()
                    except OSError:
                        pass  # 原故障仍返回unknown；一把流关闭失败不能跳过其它FD。
                _PORTABLE.clear()
                raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return 'activity_unknown'
