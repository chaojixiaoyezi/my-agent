"""用量目录的可重建来源投影；canonical JSONL/分段仍拥有事件及费用事实。

写者先使原事件索引提交，再更新来源时间界。旧库存只由显式有界prepare迁移；
只读范围查询既不创建数据库，也不列举全部历史线程。
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import locked_json_path, write_private_json_file_atomic_unlocked
from ..common.nofollow_fs import ensure_private_dir, read_bytes_beneath, unlink_file_beneath
from ..io.sqlite_probe import sqlite_readonly_probe_uri
from ..io.sqlite_scope import scoped_connection
from .observation_index import _identity


def _stamp(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('usage source directory is not a real directory')
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _text(value):
    return json.dumps(value, separators=(',', ':'))


class UsageSourcePlan:
    """只保存已核代次与时间范围，重读时仍须核完整性及每源canonical版本。"""
    def __init__(self, index, since, until, generation):
        self.index, self.since, self.until, self.generation = index, since, until, generation

    def rows(self):
        with self.index.read_connection() as conn:
            self.index.require_complete(conn, self.generation)
            cursor = conn.execute('SELECT thread_id,revision FROM sources WHERE maximum>=? AND minimum<? '
                                  'ORDER BY maximum,thread_id', (self.since, self.until))
            while True:
                rows = cursor.fetchmany(128)
                if not rows:
                    break
                for row in rows:
                    yield row['thread_id'], tuple(json.loads(row['revision']))
            self.index.require_complete(conn, self.generation)


class UsageSourceIndex:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory.parent / ('.' + self.directory.name + '_sources') / 'sources.sqlite3'
        self.pending_directory = self.path.parent / 'pending'

    def stamps(self):
        pending = _stamp(self.pending_directory)
        return _text((_stamp(self.directory), _stamp(self.directory / 'archive'), pending[:2] if pending else None))

    def _projection_rebuild_proof(self):
        """EX重建只允许绑定仍存在的canonical来源；新投影仍须prepare，不能把丢源变空完整。"""
        directory = _stamp(self.directory)
        if directory is None:
            return None
        return {'directory': directory, 'archive': _stamp(self.directory / 'archive'),
                'pending': _stamp(self.pending_directory)}

    def _open(self):
        conn = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=0.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA cache_size=-1024')
            conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS sources(thread_id TEXT PRIMARY KEY,minimum REAL,maximum REAL,'
                         'revision TEXT NOT NULL,complete INTEGER NOT NULL,epoch INTEGER NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS sources_window ON sources(maximum,thread_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS sources_incomplete ON sources(thread_id) WHERE complete=0')
            conn.execute('CREATE INDEX IF NOT EXISTS sources_epoch ON sources(epoch,thread_id)')
            conn.commit()
            return conn
        except BaseException as error:
            try:
                conn.close()
            except BaseException as cleanup:
                object.__setattr__(error, 'sqlite_cleanup_error', cleanup)
                object.__setattr__(error, 'sqlite_unclosed_factory_connection', conn)
            raise

    @contextmanager
    def write_connection(self):
        ensure_private_dir(self.path.parent)
        with locked_json_path(self.path, blocking=False):
            if not self.path.exists():
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.close(fd)
            _identity(self.path)
            with scoped_connection(self.path, False, self._open) as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise

    @contextmanager
    def read_connection(self):
        before = _identity(self.path)
        if before is None:
            raise ValueError('usage source index unavailable; coverage=unknown')
        def connect():
            conn = sqlite3.connect(sqlite_readonly_probe_uri(self.path), uri=True, timeout=0.0)
            try:
                conn.row_factory = sqlite3.Row
                return conn
            except BaseException as error:
                try:
                    conn.close()
                except BaseException as cleanup:
                    object.__setattr__(error, 'sqlite_cleanup_error', cleanup)
                    object.__setattr__(error, 'sqlite_unclosed_factory_connection', conn)
                raise
        with scoped_connection(self.path, True, connect) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            try:
                current = _identity(self.path)
                if current is None or current[:2] != before[:2]:
                    raise ValueError('usage source index changed; coverage=unknown')
                yield conn
            finally:
                conn.rollback()

    @staticmethod
    def _get(conn, key, default=''):
        row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _put(conn, key, value):
        conn.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def require_complete(self, conn, generation=None):
        if (self._get(conn, 'complete') != '1' or self._get(conn, 'stamp') != self.stamps()
                or (generation is not None and self._get(conn, 'generation', '0') != generation)
                or conn.execute('SELECT 1 FROM sources WHERE complete=0 LIMIT 1').fetchone() is not None
                or self._pending_one() is not None):
            raise ValueError('usage source index incomplete; coverage=unknown')

    def plan(self, *, since, until):
        with self.read_connection() as conn:
            self.require_complete(conn)
            return UsageSourcePlan(self, since, until, self._get(conn, 'generation', '0'))

    def _pending_record(self, token):
        if type(token) is not str or len(token) != 32 or any(ch not in '0123456789abcdef' for ch in token):
            raise ValueError('usage source intent name invalid')
        raw = read_bytes_beneath(self.pending_directory, (token + '.json',), max_bytes=4096)
        if raw is None:
            return None
        record = json.loads(raw)
        if type(record) is not dict or set(record) != {'thread_id'} or type(record['thread_id']) is not str:
            raise ValueError('usage source intent invalid')
        return record

    def _pending_one(self):
        try:
            with os.scandir(self.pending_directory) as entries:
                for _ in range(128):
                    entry = next(entries, None)
                    if entry is None:
                        return None
                    if entry.name.startswith('.') and entry.name.endswith('.tmp'):
                        continue  # 原子发表前的临时文件不可能已允许canonical追加。
                    if not entry.name.endswith('.json'):
                        raise ValueError('usage source intent name invalid')
                    token = entry.name[:-5]
                    record = self._pending_record(token)
                    if record is not None:
                        return {'token': token, **record}
                raise ValueError('usage source pending probe incomplete')
        except FileNotFoundError:
            return None

    def begin(self, thread_id):
        # 独立私有intent是唯一待提交证明，不等待来源SQLite锁，也不与其它source争抢全局写锁。
        token = uuid.uuid4().hex
        write_private_json_file_atomic_unlocked(self.pending_directory / (token + '.json'), {'thread_id': thread_id})
        return token

    def update(self, thread_id, snapshot, *, before_stamp=None, epoch=None, token=None, store=None):
        with self.write_connection() as conn:
            result = self._install(conn, thread_id, snapshot, before_stamp=before_stamp, epoch=epoch, token=token, store=store)
        if token is not None:
            try:
                unlink_file_beneath(self.pending_directory, (token + '.json',))
            except FileNotFoundError:
                pass
        return result

    def _install(self, conn, thread_id, snapshot, *, before_stamp=None, epoch=None, token=None, store=None):
        current_epoch = int(self._get(conn, 'epoch', '0'))
        if epoch is not None and current_epoch != epoch:
            return False
        if snapshot is not None and store is not None and tuple(snapshot[2]) != tuple(store.revision(thread_id)):
            snapshot = (None, None, tuple(store.revision(thread_id)), False)
        if snapshot is None:
            conn.execute('DELETE FROM sources WHERE thread_id=?', (thread_id,))
        else:
            minimum, maximum, revision, complete = snapshot
            conn.execute('INSERT INTO sources VALUES(?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET '
                         'minimum=excluded.minimum,maximum=excluded.maximum,revision=excluded.revision,'
                         'complete=excluded.complete,epoch=excluded.epoch',
                         (thread_id, minimum, maximum, _text(revision), int(complete), current_epoch if epoch is None else epoch))
        intent = self._pending_record(token) if token is not None else None
        registered = intent is not None and intent['thread_id'] == thread_id
        scan = json.loads(self._get(conn, 'scan', '{}'))
        if registered and scan and scan.get('stamp') == before_stamp:
            # 只承认同一pending写者的已登记变更；新源已带当前epoch，不因忙owner反复回到目录前缀。
            scan['stamp'] = self.stamps()
            self._put(conn, 'scan', _text(scan))
        if self._get(conn, 'complete') == '1' and self._get(conn, 'stamp') == before_stamp and registered:
            self._put(conn, 'stamp', self.stamps())
        else:
            self._put(conn, 'complete', 0)
        self._put(conn, 'generation', int(self._get(conn, 'generation', '0')) + 1)

    def _prune_unpublished_temps(self, limit):
        examined, deleted = 0, 0
        _stamp(self.pending_directory)
        try:
            with os.scandir(self.pending_directory) as entries:
                for _ in range(limit):
                    entry = next(entries, None)
                    if entry is None:
                        break
                    examined += 1
                    parts = entry.name.split('.')
                    own_temp = (len(parts) == 5 and parts[0] == '' and parts[2] == 'json' and parts[4] == 'tmp'
                                and all(len(parts[index]) == 32 and all(ch in '0123456789abcdef' for ch in parts[index])
                                        for index in (1, 3)))
                    if own_temp and entry.stat(follow_symlinks=False).st_mtime <= time.time() - 300:
                        unlink_file_beneath(self.pending_directory, (entry.name,))
                        deleted += 1
        except FileNotFoundError:
            pass
        return examined, deleted

    def prepare(self, store, *, limit=16, deadline=None):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('usage source prepare limit must be 1..128')
        try:
            if deadline is not None and time.monotonic() >= deadline:
                return {'coverage': 'partial', 'sources_examined': 0, 'cursor': {'budget_exhausted': True}, 'errors': []}
            examined, deleted = self._prune_unpublished_temps(limit)
            if deleted:
                return {'coverage': 'partial', 'sources_examined': examined, 'cursor': {'unpublished_temps': True}, 'errors': []}
            return self._prepare(store, limit=limit, deadline=deadline)
        except BlockingIOError:
            return {'coverage': 'partial', 'sources_examined': 0, 'cursor': {'busy': True}, 'errors': []}
        except sqlite3.Error as error:
            code = getattr(error, 'sqlite_errorcode', 0) & 255
            if code in (5, 6):
                return {'coverage': 'partial', 'sources_examined': 0, 'cursor': {'busy': True}, 'errors': []}
            return {'coverage': 'unknown', 'sources_examined': 0, 'cursor': {'retry': True},
                    'errors': [{'code': 'USAGE_SOURCE_INDEX_UNREADABLE'}]}
        except (OSError, ValueError, RuntimeError):
            return {'coverage': 'unknown', 'sources_examined': 0, 'cursor': {'retry': True},
                    'errors': [{'code': 'USAGE_SOURCE_INDEX_UNREADABLE'}]}

    def _prepare(self, store, *, limit=16, deadline=None):
        """最多limit个目录/源步骤；目录cookie持久化在本索引meta，重启不重扫前缀。"""
        from ..user_space.directory_paging import directory_page

        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('usage source prepare limit must be 1..128')
        if deadline is not None and time.monotonic() >= deadline:
            return {'coverage': 'partial', 'sources_examined': 0, 'cursor': {'budget_exhausted': True}, 'errors': []}
        with self.write_connection() as conn:
            complete = self._get(conn, 'complete') == '1' and self._get(conn, 'stamp') == self.stamps()
            writing = self._pending_one()
            after = self._get(conn, 'repair_after')
            pending = conn.execute('SELECT thread_id FROM sources WHERE complete=0 AND thread_id>? ORDER BY thread_id LIMIT 1',
                                   (after,)).fetchone()
            if pending is None:
                pending = conn.execute('SELECT thread_id FROM sources WHERE complete=0 ORDER BY thread_id LIMIT 1').fetchone()
            if complete and pending is None and writing is None:
                return {'coverage': 'complete', 'sources_examined': 0, 'cursor': None, 'errors': []}
            if pending is not None:
                self._put(conn, 'repair_after', pending[0])
        if writing is not None or (complete and pending is not None):
            identifier = writing['thread_id'] if writing is not None else pending[0]
            token = writing['token'] if writing is not None else None
            try:
                store._reconcile_usage_source(identifier, token=token)
                return {'coverage': 'partial', 'sources_examined': 1, 'cursor': {'pending_source': identifier}, 'errors': []}
            except BlockingIOError:
                return {'coverage': 'partial', 'sources_examined': 1, 'cursor': {'busy': True}, 'errors': []}
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                return {'coverage': 'unknown', 'sources_examined': 1, 'cursor': {'pending_source': identifier},
                        'errors': [{'code': 'USAGE_SOURCE_UNREADABLE', 'thread_id': identifier}]}
        work, errors = 0, []
        with self.write_connection() as conn:
            state = json.loads(self._get(conn, 'scan', '{}'))
            if not state:
                epoch = int(self._get(conn, 'epoch', '0')) + 1
                state = {'phase': 0, 'epoch': epoch, 'stamp': self.stamps(), 'cursor': {}}
                self._put(conn, 'epoch', epoch)
                self._put(conn, 'complete', 0)
            while work < limit and (deadline is None or time.monotonic() < deadline):
                if state['phase'] == 2:
                    if state['stamp'] != self.stamps():
                        state = {}
                        break
                    stale = conn.execute('SELECT thread_id FROM sources WHERE epoch<? ORDER BY epoch,thread_id LIMIT ?',
                                         (state['epoch'], limit - work)).fetchall()
                    for row in stale:
                        conn.execute('DELETE FROM sources WHERE thread_id=? AND epoch<?', (row[0], state['epoch']))
                    work += max(1, len(stale))
                    if not stale:
                        self._put(conn, 'stamp', self.stamps())
                        self._put(conn, 'complete', 1)
                        self._put(conn, 'generation', int(self._get(conn, 'generation', '0')) + 1)
                        state = {}
                        break
                    continue
                parts = (self.directory.name,) if state['phase'] == 0 else (self.directory.name, 'archive')
                names, cookie, finished = directory_page(self.directory.parent, parts, state['cursor'], limit - work)
                work += max(1, cookie.pop('last_scanned', len(names)))
                state['cursor'] = cookie
                for name in names:
                    identifier = Path(name).stem if state['phase'] == 0 and name.endswith('.jsonl') else (
                        name if state['phase'] == 1 else None)
                    if identifier is None:
                        continue
                    try:
                        snapshot = store._usage_source_snapshot(identifier, prepare=False)
                        self._install(conn, identifier, snapshot, epoch=state['epoch'], store=store)
                    except (OSError, ValueError, RuntimeError, sqlite3.Error):
                        self._install(conn, identifier, (None, None, (), False), epoch=state['epoch'])
                        errors.append({'code': 'USAGE_SOURCE_UNREADABLE', 'thread_id': identifier})
                if finished:
                    state['phase'] += 1
                    state['cursor'] = {}
                    if state['phase'] == 2 and state['stamp'] != self.stamps():
                        state = {}
                        break
            self._put(conn, 'scan', _text(state))
            ready = (not state and self._get(conn, 'complete') == '1' and self._get(conn, 'stamp') == self.stamps()
                     and conn.execute('SELECT 1 FROM sources WHERE complete=0 LIMIT 1').fetchone() is None
                     and self._pending_one() is None)
        return {'coverage': 'complete' if ready else 'unknown' if errors else 'partial', 'sources_examined': work,
                'cursor': None if ready else {'phase': state.get('phase', 0), 'epoch': state.get('epoch', 0)}, 'errors': errors}
