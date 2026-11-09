"""检查点物理行地址的可重建投影；只封存已完成严格扫描的固定源水位。"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..io.sqlite_scope import scoped_connection, sqlite_connection_factory


def validator_identity(validator):
    if validator is None:
        return 'unvalidated:v1'
    try:
        source = Path(inspect.getsourcefile(validator)).read_bytes()
        return validator.__module__ + ':' + validator.__qualname__ + ':' + hashlib.sha256(source).hexdigest()
    except (TypeError, OSError, AttributeError):
        # 动态/闭包 validator 没有稳定实现身份，不复用跨进程校验结果。
        return None


class CheckpointOffsetStore:
    def __init__(self, ledger):
        self.path = ledger.with_name('.' + ledger.name + '.offsets.sqlite3')

    @contextmanager
    def connection(self, *, write=True):
        def connect():
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                if not write:
                    raise
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    info = self.path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise OSError('untrusted checkpoint offset projection') from None
                else:
                    os.close(descriptor)
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError('untrusted checkpoint offset projection')
            if not write:
                before = self.path.lstat()
                descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                     | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
                try:
                    opened = os.fstat(descriptor)
                    def identity(value):
                        return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
                                value.st_mtime_ns)
                    def version(value):
                        return (*identity(value), value.st_ctime_ns)
                    # Windows的path ctime是创建时间，FD ctime是元数据变更时间。
                    # 身份跨读口核对，版本只在各自读口前后比较，不能把两种时钟当成相同事实。
                    cross_identity = identity if os.name == 'nt' else version
                    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                            or cross_identity(opened) != cross_identity(before)
                            or os.read(descriptor, 16) != b'SQLite format 3\x00'
                            or version(os.fstat(descriptor)) != version(opened)
                            or version(self.path.lstat()) != version(before)):
                        raise OSError('untrusted checkpoint offset projection header')
                finally:
                    os.close(descriptor)
            mode = 'rw' if write else 'ro'
            conn = sqlite3.connect(self.path.absolute().as_uri() + '?mode=' + mode, uri=True, timeout=5,
                                   factory=sqlite_connection_factory())
            try:
                if write:
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('PRAGMA synchronous=FULL')
                    conn.executescript('''CREATE TABLE IF NOT EXISTS offsets (
                        offset INTEGER PRIMARY KEY, identity TEXT NOT NULL, length INTEGER NOT NULL, digest BLOB NOT NULL);
                        CREATE INDEX IF NOT EXISTS offsets_identity ON offsets(identity,offset DESC);
                        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);''')
                return conn
            except BaseException as primary:
                try:
                    conn.close()
                except BaseException as cleanup:
                    primary.sqlite_cleanup_error = cleanup
                    primary.sqlite_unclosed_factory_connection = conn
                raise

        with scoped_connection(self.path, read_only=not write, connect=connect) as conn:
            try:
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield conn
            finally:
                conn.rollback()

    def retire_broken(self):
        from ..io.sqlite_scope import retire_scoped_database
        from ..runtime_db.recovery_guard import runtime_db_generation_guard

        retire_scoped_database(self.path)
        with runtime_db_generation_guard(self.path, exclusive=True):
            # 不复用坏库文件；原库及 WAL/SHM 作为同一代证据保留，稳定锁锚不移动。
            suffix = '.damaged-' + uuid.uuid4().hex
            for extension in ('', '-wal', '-shm'):
                source = self.path.with_name(self.path.name + extension)
                try:
                    source.rename(source.with_name(source.name + suffix))
                except FileNotFoundError:
                    pass

    @staticmethod
    def load(conn, validator):
        identity = validator_identity(validator)
        row = conn.execute("SELECT value FROM metadata WHERE key='snapshot'").fetchone()
        if row is None or identity is None:
            return None
        try:
            data = json.loads(row[0])
            if set(data) != {'schema', 'stamp', 'resume', 'head', 'tail', 'validator'} or data['schema'] != 1:
                return None
            if data['validator'] != identity:
                return None
            stamp = data['stamp']
            if type(stamp) is not list or len(stamp) != 5 or any(type(item) is not int or item < 0 for item in stamp):
                return None
            if type(data['resume']) is not int or not 0 <= data['resume'] <= stamp[2]:
                return None
            return data
        except (ValueError, TypeError):
            return None

    @staticmethod
    def begin(conn, start):
        conn.execute('DELETE FROM offsets WHERE offset>=?', (start,))

    @staticmethod
    def add(conn, identity, offset, raw):
        conn.execute('INSERT INTO offsets VALUES(?,?,?,?)',
                     (offset, identity, len(raw), hashlib.sha256(raw).digest()))

    @staticmethod
    def publish(conn, index, validator):
        identity = validator_identity(validator)
        data = {'schema': 1, 'stamp': index.stamp, 'resume': index.resume_from,
                'head': index.head_digest.hex(), 'tail': index.tail_digest.hex(), 'validator': identity}
        conn.execute("INSERT INTO metadata VALUES('snapshot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(data),))
        conn.commit()
        # lookup 在已发布投影的独立读快照中查询，后来的追加不能改变本次last-wins。
        conn.execute('BEGIN')
        conn.execute("SELECT value FROM metadata WHERE key='snapshot'").fetchone()

    @staticmethod
    def lookup(conn, identity, stamp):
        through = stamp[2]
        saved = conn.execute("SELECT value FROM metadata WHERE key='snapshot'").fetchone()
        try:
            projection_stamp = tuple(json.loads(saved[0])['stamp'])
        except (TypeError, ValueError, KeyError) as exc:
            raise OSError('checkpoint offset projection snapshot is invalid') from exc
        if (projection_stamp[:2] != stamp[:2] or projection_stamp[2] < through
                or (projection_stamp[2] == through and projection_stamp != stamp)):
            raise OSError('checkpoint offset projection source changed during reading')
        # 同水位不加 offset 过滤，越界地址必须显式失败，不能被 WHERE 隐藏成缺失。
        if projection_stamp[2] == through:
            row = conn.execute('SELECT offset,length,digest FROM offsets WHERE identity=? '
                               'ORDER BY offset DESC LIMIT 1', (identity,)).fetchone()
        else:
            row = conn.execute('SELECT offset,length,digest FROM offsets WHERE identity=? AND offset<? '
                               'ORDER BY offset DESC LIMIT 1', (identity, through)).fetchone()
        if row is None:
            return None
        offset, length, digest = row
        if (type(offset) is not int or type(length) is not int or offset < 0 or length <= 0
                or offset + length > through or type(digest) is not bytes or len(digest) != 32):
            raise OSError('checkpoint offset projection address is invalid')
        return offset, length, digest
