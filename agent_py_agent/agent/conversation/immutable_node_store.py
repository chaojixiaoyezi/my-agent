"""内容寻址节点的事务仓库：历史根共享节点，零引用队列代替全历史标记。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from ..common import nofollow_fs as fs
from ..io.sqlite_scope import scoped_connection, sqlite_connection_factory

DATABASE_NAME = '.nodes.sqlite3'
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS nodes (
 name TEXT PRIMARY KEY, raw BLOB, refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0), touched REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_garbage ON nodes(refs,touched,name);
CREATE INDEX IF NOT EXISTS nodes_missing ON nodes(name) WHERE raw IS NULL;
CREATE TRIGGER IF NOT EXISTS nodes_keep_live BEFORE DELETE ON nodes WHEN OLD.refs>0
 BEGIN SELECT RAISE(ABORT, 'cannot delete referenced immutable node'); END;
CREATE TRIGGER IF NOT EXISTS nodes_immutable BEFORE UPDATE OF raw ON nodes
 WHEN OLD.raw IS NOT NULL AND NEW.raw IS NOT OLD.raw
 BEGIN SELECT RAISE(ABORT, 'cannot overwrite immutable node'); END;
CREATE TABLE IF NOT EXISTS edges (
 parent TEXT NOT NULL, child TEXT NOT NULL, PRIMARY KEY(parent,child)
);
CREATE TABLE IF NOT EXISTS pins (
 checkpoint TEXT NOT NULL, name TEXT NOT NULL, generation INTEGER NOT NULL, committed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(checkpoint,name)
);
CREATE INDEX IF NOT EXISTS pins_candidates ON pins(committed,generation,checkpoint);
CREATE TABLE IF NOT EXISTS files (namespace TEXT NOT NULL, name TEXT NOT NULL, refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0), touched REAL NOT NULL,
 PRIMARY KEY(namespace,name));
CREATE INDEX IF NOT EXISTS files_garbage ON files(refs,touched,namespace,name);
CREATE TABLE IF NOT EXISTS objects (checkpoint TEXT NOT NULL, namespace TEXT NOT NULL, name TEXT NOT NULL,
 generation INTEGER NOT NULL, committed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(checkpoint,namespace,name));
CREATE INDEX IF NOT EXISTS objects_names ON objects(namespace,name);
CREATE INDEX IF NOT EXISTS objects_candidates ON objects(committed,generation);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


class ImmutableNodeStore:
    """只存放索引节点；checkpoint/thread CAS 仍是提交事实源。"""

    def __init__(self, root: Path, parts: tuple[str, ...]):
        self.root, self.parts = Path(root), parts
        self.path = self.root.joinpath(*parts, DATABASE_NAME)

    def exists(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise fs.NoFollowPathError('immutable node database must be regular single-link')
        return True

    @contextmanager
    def connection(self, *, write=False):
        # 所有物理连接共用 generation 租约；只读不创建目录、数据库或 schema。
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = fs.open_directory_beneath(self.root, self.parts, create=True)
            os.close(descriptor)
        else:
            descriptor = fs.open_directory_beneath(self.root, self.parts)
            os.close(descriptor)
        present = self.exists()
        if not present and not write:
            raise FileNotFoundError(self.path)
        def connect():
            # 物理库出生在 generation SH 租约之内，不能与 EX 删除/恢复交错空建。
            if not self.exists():
                if not write:
                    raise FileNotFoundError(self.path)
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    self.exists()
                else:
                    os.close(descriptor)
            mode = 'rw' if write else 'ro'
            conn = sqlite3.connect(self.path.absolute().as_uri() + '?mode=' + mode, uri=True,
                                   timeout=5, factory=sqlite_connection_factory())
            conn.execute('PRAGMA busy_timeout=5000')
            if write:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=FULL')
                conn.executescript(_SCHEMA)
            return conn

        with scoped_connection(self.path, read_only=not write, connect=connect) as conn:
            try:
                if write:
                    conn.execute('BEGIN IMMEDIATE')
                yield conn
                if write:
                    conn.commit()
            except BaseException:
                if write:
                    conn.rollback()
                raise

    def read(self, name):
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute('SELECT raw FROM nodes WHERE name=?', (name,)).fetchone()
        return None if row is None or row[0] is None else bytes(row[0])

    @staticmethod
    def _insert(conn, name, raw, children, now):
        old = conn.execute('SELECT raw FROM nodes WHERE name=?', (name,)).fetchone()
        if old is not None and old[0] is not None:
            if bytes(old[0]) != raw:
                raise OSError('immutable index node digest mismatches')
            conn.execute('UPDATE nodes SET touched=max(touched,?) WHERE name=?', (now, name))
            return
        conn.execute('INSERT INTO nodes(name,raw,touched) VALUES(?,?,?) '
                     'ON CONFLICT(name) DO UPDATE SET raw=excluded.raw,touched=excluded.touched', (name, raw, now))
        for child in set(children):
            conn.execute('INSERT INTO nodes(name,touched) VALUES(?,?) ON CONFLICT(name) DO NOTHING', (child, now))
            conn.execute('INSERT INTO edges(parent,child) VALUES(?,?)', (name, child))
            conn.execute('UPDATE nodes SET refs=refs+1 WHERE name=?', (child,))

    def _require_node(self, conn, name):
        row = conn.execute('SELECT raw IS NOT NULL FROM nodes WHERE name=?', (name,)).fetchone()
        if row is not None and row[0]:
            return
        # 新父节点/新pin与零引用GC在同一事务串行；进程暂停超过宽限期后不能发表缺子树。
        raw = fs.read_bytes_beneath(self.root, (*self.parts, name), max_bytes=1024 * 1024)
        if raw is None:
            raise FileNotFoundError('immutable node disappeared before publication')
        verify_node_bytes(name, raw)

    def save(self, name, raw, children, *, touched=None):
        with self.connection(write=True) as conn:
            for child in set(children):
                self._require_node(conn, child)
            self._insert(conn, name, raw, children, time.time() if touched is None else touched)

    def pin(self, checkpoint, generation, references, *, committed=False, objects=(), object_validator=None):
        names = {ref['ref']['ref'] for ref in references if ref is not None}
        with self.connection(write=True) as conn:
            for name in names:
                self._require_node(conn, name)
                # 旧节点可尚未搬迁，placeholder 的入边计数也属于同一事务。
                conn.execute('INSERT INTO nodes(name,touched) VALUES(?,?) ON CONFLICT(name) DO NOTHING', (name, time.time()))
                inserted = conn.execute('INSERT OR IGNORE INTO pins(checkpoint,name,generation,committed) VALUES(?,?,?,?)',
                                        (checkpoint, name, generation, int(committed))).rowcount
                if inserted:
                    conn.execute('UPDATE nodes SET refs=refs+1 WHERE name=?', (name,))
            for namespace, name in objects:
                if object_validator is None:
                    raise ValueError('object pins require a physical existence validator')
                object_validator(namespace, name)
                inserted = conn.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?)',
                                        (checkpoint, namespace, name, generation, int(committed))).rowcount
                if inserted:
                    conn.execute('INSERT INTO files VALUES(?,?,1,?) ON CONFLICT(namespace,name) DO UPDATE SET refs=refs+1',
                                 (namespace, name, time.time()))
            if committed:
                conn.execute('UPDATE objects SET committed=1 WHERE checkpoint=?', (checkpoint,))
                conn.execute('UPDATE pins SET committed=1 WHERE checkpoint=?', (checkpoint,))

    def state(self, key):
        if not self.exists():
            return ''
        with self.connection() as conn:
            row = conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return row[0] if row else ''

    def set_state(self, key, value):
        with self.connection(write=True) as conn:
            conn.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def collect(self, *, generation, cutoff, limit):
        """仅在规范 head 链已注册后调用；CAS 已落后候选才可解除 pin。"""
        deleted = 0
        with self.connection(write=True) as conn:
            candidates = conn.execute('SELECT checkpoint,name FROM pins WHERE committed=0 AND generation<=? LIMIT ?',
                                      (generation, limit)).fetchall()
            for checkpoint, name in candidates:
                conn.execute('DELETE FROM pins WHERE checkpoint=? AND name=? AND committed=0', (checkpoint, name))
                conn.execute('UPDATE nodes SET refs=refs-1 WHERE name=?', (name,))
            object_candidates = conn.execute('SELECT checkpoint,namespace,name FROM objects WHERE committed=0 AND generation<=? LIMIT ?',
                                             (generation, max(0, limit-len(candidates)))).fetchall()
            for checkpoint, namespace, name in object_candidates:
                conn.execute('DELETE FROM objects WHERE checkpoint=? AND namespace=? AND name=?', (checkpoint, namespace, name))
                conn.execute('UPDATE files SET refs=refs-1 WHERE namespace=? AND name=?', (namespace, name))
            remaining = max(0, limit - len(candidates) - len(object_candidates))
            rows = conn.execute('SELECT name FROM nodes WHERE refs=0 AND touched<? LIMIT ?',
                                (cutoff, remaining)).fetchall()
            for (name,) in rows:
                children = conn.execute('SELECT child FROM edges WHERE parent=?', (name,)).fetchall()
                conn.execute('DELETE FROM edges WHERE parent=?', (name,))
                conn.execute('DELETE FROM nodes WHERE name=? AND refs=0', (name,))
                for (child,) in children:
                    conn.execute('UPDATE nodes SET refs=refs-1 WHERE name=?', (child,))
                deleted += 1
        # SQLite 空闲页供后续写入复用，不把逻辑正文删除谎报成宿主磁盘释放。
        return len(candidates) + len(object_candidates) + deleted, deleted, 0


    def referenced_names(self, names):
        if not names:
            return set()
        with self.connection() as conn:
            placeholders = ','.join('?' for _ in names)
            return {row[0] for row in conn.execute(f'SELECT name FROM nodes WHERE refs>0 AND name IN ({placeholders})', names)}

    def reference_count(self, name):
        with self.connection() as conn:
            row = conn.execute('SELECT refs FROM nodes WHERE name=?', (name,)).fetchone()
        return row[0] if row else 0

    def has_due(self, generation, cutoff):
        with self.connection() as conn:
            return any((
                conn.execute('SELECT 1 FROM nodes WHERE refs=0 AND touched<? LIMIT 1', (cutoff,)).fetchone(),
                conn.execute('SELECT 1 FROM pins WHERE committed=0 AND generation<=? LIMIT 1', (generation,)).fetchone(),
                conn.execute('SELECT 1 FROM objects WHERE committed=0 AND generation<=? LIMIT 1', (generation,)).fetchone(),
                conn.execute('SELECT 1 FROM files WHERE refs=0 AND touched<? LIMIT 1', (cutoff,)).fetchone(),
            ))

    def track_file(self, namespace, name):
        with self.connection(write=True) as conn:
            conn.execute('INSERT INTO files(namespace,name,touched) VALUES(?,?,?) '
                         'ON CONFLICT(namespace,name) DO UPDATE SET touched=excluded.touched',
                         (namespace, name, time.time()))

    def garbage_files(self, cutoff, limit):
        with self.connection() as conn:
            return conn.execute('SELECT namespace,name FROM files WHERE refs=0 AND touched<? LIMIT ?', (cutoff, limit)).fetchall()

    def forget_file(self, namespace, name):
        with self.connection(write=True) as conn:
            conn.execute('DELETE FROM files WHERE namespace=? AND name=? AND refs=0', (namespace, name))

    def retained_objects(self, namespace, names):
        if not names:
            return set()
        with self.connection() as conn:
            placeholders = ','.join('?' for _ in names)
            rows = conn.execute(f'SELECT DISTINCT name FROM objects WHERE namespace=? AND name IN ({placeholders})',
                                (namespace, *names)).fetchall()
        return {row[0] for row in rows}

def node_children(raw):
    return [value['ref']['ref'] for value in json.loads(raw).get('children', {}).values()]


def verify_node_bytes(name, raw):
    if name != hashlib.sha256(raw).hexdigest() + '.json':
        raise OSError('immutable index node digest mismatches')
