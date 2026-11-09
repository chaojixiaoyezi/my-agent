"""owner 温缓存的单路径清单/stat 投影；远端 version 与观察器 clock 同事务提交。"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .io.sqlite_scope import scoped_connection, sqlite_connection_factory
from .owner_snapshot_index import OwnerStatIndex, StatEntry


class OwnerSnapshotIndex(OwnerStatIndex):
    def __init__(self, conn):
        self.conn = conn

    @classmethod
    @contextmanager
    def open(cls, path):
        path.parent.mkdir(parents=True, exist_ok=True)

        def connect():
            try:
                info = path.lstat()
            except FileNotFoundError:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError('owner snapshot index must be regular single-link')
            conn = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True, timeout=5,
                                   factory=sqlite_connection_factory())
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=FULL')
                conn.executescript('''CREATE TABLE IF NOT EXISTS entries(path TEXT PRIMARY KEY, stat TEXT, manifest TEXT);
                    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);''')
                return conn
            except BaseException as primary:
                try:
                    conn.close()
                except BaseException as cleanup:
                    primary.sqlite_cleanup_error = cleanup
                    primary.sqlite_unclosed_factory_connection = conn
                raise

        with scoped_connection(path, read_only=False, connect=connect) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield cls(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def retire_projection(path):
        from .io.sqlite_scope import retire_scoped_database
        from .runtime_db.recovery_guard import runtime_db_generation_guard

        retire_scoped_database(path)
        with runtime_db_generation_guard(path, exclusive=True):
            generation = '.damaged-' + uuid4().hex
            for extension in ('', '-wal', '-shm'):
                source = path.with_name(path.name + extension)
                try:
                    source.rename(source.with_name(source.name + generation))
                except FileNotFoundError:
                    pass

    def lookup(self, relative_path, st):
        row = self.conn.execute('SELECT stat FROM entries WHERE path=?', (relative_path,)).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            entry = StatEntry(**json.loads(row[0]))
        except (ValueError, TypeError):
            return None
        actual = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
        expected = (entry.dev, entry.ino, entry.size, entry.mtime_ns, entry.ctime_ns, entry.mode)
        return entry.sha256 if actual == expected and not entry.racy else None

    def record(self, relative_path, st, sha256, written_at):
        entry = StatEntry(relative_path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                          st.st_mode, sha256, abs(st.st_mtime_ns / 1e9 - written_at) < 2)
        self.conn.execute('INSERT INTO entries(path,stat) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET stat=excluded.stat',
                          (relative_path, json.dumps(asdict(entry))))

    def forget(self, relative_path):
        self.conn.execute('DELETE FROM entries WHERE path=?', (relative_path,))

    def paths(self):
        return {row[0] for row in self.conn.execute('SELECT path FROM entries')}

    def manifest(self, paths=None):
        if paths is None:
            rows = self.conn.execute('SELECT path,manifest FROM entries WHERE manifest IS NOT NULL')
            return {path: json.loads(raw) for path, raw in rows}
        result = {}
        values = list(paths)
        for start in range(0, len(values), 128):
            batch = values[start:start + 128]
            placeholders = ','.join('?' for _ in batch)
            rows = self.conn.execute(f'SELECT path,manifest FROM entries WHERE manifest IS NOT NULL AND path IN ({placeholders})', batch)
            result.update((path, json.loads(raw)) for path, raw in rows)
        return result

    def subtree(self, prefix):
        # 二进制范围不依赖 LIKE 的大小写或 Unicode 转义语义。
        return {row[0] for row in self.conn.execute('SELECT path FROM entries WHERE path=? OR (path>=? AND path<?)',
                                                   (prefix, prefix + '/', prefix + '0'))}

    def set_manifest(self, row):
        self.conn.execute('INSERT INTO entries(path,manifest) VALUES(?,?) '
                          'ON CONFLICT(path) DO UPDATE SET manifest=excluded.manifest',
                          (row.relative_path, json.dumps(asdict(row))))

    def reset(self, rows):
        self.conn.execute('DELETE FROM entries')
        self.conn.executemany('INSERT INTO entries(path,manifest) VALUES(?,?)',
                              ((row.relative_path, json.dumps(asdict(row))) for row in rows))

    def metadata(self):
        row = self.conn.execute("SELECT value FROM state WHERE key='committed'").fetchone()
        try:
            data = json.loads(row[0]) if row else {}
            return data if type(data) is dict else {}
        except ValueError:
            return {}

    def set_metadata(self, value):
        self.conn.execute("INSERT INTO state VALUES('committed',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (json.dumps(value),))
