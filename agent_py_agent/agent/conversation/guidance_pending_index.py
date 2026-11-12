# LLM: 回执和队列仍是权威；SQLite仅定位，退休事实独立追加，重建必须固定切片并显式报告partial。
# 模块用途: 为插话提供精确送达事实、队列位置和按目标/状态/次序排列的待办投影。
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import (
    append_private_jsonl_records_unlocked,
    check_io_deadline,
    locked_json_path,
)
from ..common.nofollow_fs import ensure_private_dir
from ..gateway_parts.io import locked_file_transition
from ..io.sqlite_scope import connect_projection_sqlite, scoped_connection
from ..runtime_errors import DataCorruptionError
from ..user_space.home_indexes import is_sqlite_busy
from .models import GuidanceEntry
from .observation_index import (
    _INSTALLED,
    _INSTALLED_LOCK,
    ObservationIndex,
    _close_failed_factory,
    _directory_stamp,
    _identity,
    _legacy_slice,
    _reader,
)
from .store_guidance_records import _guidance_queue_entry_matches_receipt


# LLM: 无coverage字段的旧列表入口不能把未知退休状态当pending；重试推进同一持久游标。
# 类用途: 表示当前固定切片尚未追平权威来源。
class GuidanceIndexIncomplete(DataCorruptionError):
    pass


# LLM: 仅复用私有日志字节协议，不使用观察业务的ack/pending/迁移入口；独立路径、表与原锁序。
# 类用途: 管理插话的可丢弃定位投影和不可丢弃退休日志。
class GuidancePendingIndex(ObservationIndex):
    # LLM: 构造不产生IO；ledger提供原回执锁内的规范读取和迁移。
    # 函数用途: 绑定唯一插话存储上下文。
    def __init__(self, storage, ledger):
        self.storage = storage
        self.ledger = ledger
        self.path = storage.root / '.guidance-pending.sqlite3'
        self.journal_path = storage.root / '.guidance-delivered.jsonl'
        self.dirty_dir = storage.root / '.guidance-dirty'
        self.last_work = {}
        self._quiet = None

    # LLM: 所有新增等待共享原blocking/deadline；busy不是损坏，不得截断；每个出口关闭连接。
    # 函数用途: 打开一次私有SQLite事务，必要时仅重建损坏投影。
    @contextmanager
    def _connection(self, *, blocking=True, deadline=None):
        check_io_deadline(deadline)
        ensure_private_dir(self.path.parent)
        with locked_json_path(self.path, blocking=blocking):
            check_io_deadline(deadline)
            if not self.path.exists():
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.close(fd)
            _identity(self.path)
            os.chmod(self.path, 0o600, follow_symlinks=False)
            for suffix in ('-wal', '-shm', '-journal'):
                _identity(Path(str(self.path) + suffix))
            try:
                with scoped_connection(self.path, False, lambda: self._open_bounded(blocking, deadline),
                                       rebuild_projection=self._projection_rebuild_proof) as conn:
                    try:
                        conn.execute('BEGIN IMMEDIATE')
                        yield conn
                        check_io_deadline(deadline)
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
            except sqlite3.DatabaseError as exc:
                if is_sqlite_busy(exc):
                    raise BlockingIOError('guidance projection is busy') from exc
                raise

    def _projection_rebuild_proof(self):
        journal = _identity(self.journal_path)
        legacy = _identity(self.storage.guidance_delivered_path)
        directory = _directory_stamp(self.storage.root)
        return (journal, legacy, directory[:2]) if directory and (journal or legacy) else None

    # LLM: target/status/sequence匹配实际查询；generation不符只丢投影，独立退休日志继续可恢复。
    # 函数用途: 初始化有界等待的WAL数据库。首次安装时一次性删掉旧版本遗留的缺失来源 'null' 行。
    def _open_bounded(self, blocking, deadline):
        import time
        seconds = 5 if blocking else 0
        if deadline is not None:
            seconds = min(seconds, max(0, deadline - time.monotonic()))
        identity = _identity(self.path)
        key = (str(self.path), *identity[:2])
        conn = connect_projection_sqlite(self.path, timeout=seconds)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f'PRAGMA busy_timeout={int(seconds * 1000)}')
            conn.execute('PRAGMA synchronous=FULL')
            with _INSTALLED_LOCK:
                if key in _INSTALLED and identity[2] and self._get(conn, 'generation', '') != 'guidance_pending.v1':
                    _INSTALLED.discard(key)
                if key not in _INSTALLED or not identity[2]:
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
                    generation = self._get(conn, 'generation', '')
                    if generation != 'guidance_pending.v1':
                        for table in ('acks', 'entries', 'pending', 'sources', 'errors'):
                            conn.execute(f'DROP TABLE IF EXISTS {table}')
                        conn.execute('DELETE FROM meta')
                        self._put(conn, 'generation', 'guidance_pending.v1')
                    conn.execute('CREATE TABLE IF NOT EXISTS acks(id TEXT PRIMARY KEY,at REAL NOT NULL)')
                    conn.execute('CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,target_type TEXT,target_id TEXT,sequence INTEGER,length INTEGER,dedupe TEXT)')
                    conn.execute('CREATE INDEX IF NOT EXISTS entries_recent ON entries(target_type,target_id,sequence DESC)')
                    conn.execute('CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY,target_type TEXT,target_id TEXT,status TEXT,sequence INTEGER)')
                    conn.execute('CREATE INDEX IF NOT EXISTS pending_target ON pending(target_type,target_id,status,sequence DESC)')
                    conn.execute('CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,stamp TEXT,offset INTEGER)')
                    conn.execute('CREATE TABLE IF NOT EXISTS errors(path TEXT,offset INTEGER,error TEXT,PRIMARY KEY(path,offset))')
                    conn.execute("DELETE FROM sources WHERE stamp='null'")
                    conn.commit()
                    _INSTALLED.add(key)
            return conn
        except BaseException as error:
            _close_failed_factory(conn, error)
            raise

    # LLM: 内部无dedupe消息的正退休事实只追加；不能代替幂等回执的provider确认。
    # 函数用途: 安装从日志恢复的精确送达事实并清理内部pending候选。
    def _install(self, conn, pairs):
        conn.executemany('INSERT INTO acks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET at=excluded.at WHERE acks.at<=0', pairs)
        conn.executemany("DELETE FROM pending WHERE id=? AND ? > 0 AND EXISTS (SELECT 1 FROM entries e WHERE e.id=pending.id AND e.dedupe='')", pairs)

    # LLM: 原delivered锁必须保留try-lock；legacy只读且每次最多128项，游标与追加日志投影同事务。
    # 函数用途: 迁移旧送达map的一个固定切片。
    def _legacy_guidance(self, conn, work, *, blocking=True, deadline=None):
        path = self.storage.guidance_delivered_path
        with locked_json_path(path, blocking=blocking):
            check_io_deadline(deadline)
            identity = _identity(path)
            stamp = json.dumps(identity)
            previous = self._get(conn, 'legacy_stamp', '')
            offset = int(self._get(conn, 'legacy_offset'))
            complete = self._get(conn, 'legacy_complete') == '1'
            if identity is None:
                if previous not in ('', 'null') and offset and not complete:
                    raise DataCorruptionError('unfinished guidance delivered map is missing')
                self._put(conn, 'legacy_stamp', stamp)
                self._put(conn, 'legacy_complete', 1)
                return True
            if previous != stamp:
                offset, complete = 0, False
            if complete:
                return True
            pairs, after, complete, consumed = _legacy_slice(path, offset)
            if _identity(path) != identity:
                raise GuidanceIndexIncomplete('guidance delivered map changed during migration')
            valid = []
            for key, raw in pairs:
                try:
                    value = float(raw or 0)
                except (ValueError, TypeError):
                    continue
                if math.isfinite(value) and value > 0:
                    row = conn.execute('SELECT at FROM acks WHERE id=?', (key,)).fetchone()
                    if not row or row[0] <= 0:
                        valid.append((key, value))
            if valid:
                check_io_deadline(deadline)
                end, _written = self._append(valid)
                self._install(conn, valid)
                self._journal_checkpoint(conn, end)
            self._put(conn, 'legacy_stamp', stamp)
            self._put(conn, 'legacy_offset', after)
            self._put(conn, 'legacy_complete', int(complete))
            work['bytes_read'] += consumed
            return complete

    # LLM: 只有独立日志已fsync才移动投影游标；崩溃重放保持幂等。
    # 函数用途: 保存退休日志精确位置和身份。
    def _journal_checkpoint(self, conn, end):
        identity = _identity(self.journal_path)
        self._put(conn, 'journal_offset', end)
        self._put(conn, 'journal_identity', json.dumps(identity[:2]))
        self._put(conn, 'journal_stamp', json.dumps(identity))

    # LLM: 每次最多恢复一个日志切片和一个legacy切片；未知ID只有两源追平才可当未退休。
    # 函数用途: 推进退休事实恢复并返回完整性。
    def _prepare_acks(self, conn, *, blocking=True, deadline=None):
        work = {'bytes_read': 0, 'bytes_written': 0, 'json_decodes': 0}
        complete = self._sync(conn, work)
        check_io_deadline(deadline)
        if complete:
            complete = self._legacy_guidance(conn, work, blocking=blocking, deadline=deadline)
        self.last_work = work
        return complete

    # LLM: 只查给定ID；未迁移区间的缺失不能被解释为未送达。
    # 函数用途: 返回精确送达时间或显式partial。
    def delivered(self, guidance_id, *, blocking=True, deadline=None):
        return self.delivered_many([guidance_id], blocking=blocking, deadline=deadline).get(guidance_id, 0.0)

    # LLM: 同一有限候选批共用一次恢复探针，不对每个ID重复解析日志身份与legacy游标。
    # 函数用途: 批量精确查询请求涉及的退休事实。
    def delivered_many(self, ids, *, blocking=True, deadline=None):
        ids = tuple(ids)
        with self._read_connection(blocking=blocking, deadline=deadline) as conn:
            if conn is not None and self._acks_current(conn):
                self.last_work = {'bytes_read': 0, 'bytes_written': 0, 'json_decodes': 0}
                return self._lookup(conn, ids)
        with self._connection(blocking=blocking, deadline=deadline) as conn:
            complete = self._prepare_acks(conn, blocking=blocking, deadline=deadline)
            found = self._lookup(conn, ids)
        if not complete and any(key not in found for key in ids):
            raise GuidanceIndexIncomplete('guidance delivered recovery is partial')
        return found

    def _acks_current(self, conn):
        return (self._get(conn, 'generation', '') == 'guidance_pending.v1'
                and self._journal_current(conn)
                and self._legacy_current(conn, self.storage.guidance_delivered_path))

    # LLM: ack只受新日志恢复约束，不依赖整个legacy map；重复正ack不追加，不重写旧map。
    # 函数用途: 持久退休明确指定的内部消息ID。
    def mark_delivered(self, ids, current):
        value = float(current)
        if not math.isfinite(value):
            raise ValueError('guidance delivered time must be finite')
        with self._connection() as conn:
            work = {'bytes_read': 0, 'bytes_written': 0, 'json_decodes': 0}
            complete = self._sync(conn, work)
            if complete:
                known = self._lookup(conn, ids)
                pairs = [(key, value) for key in dict.fromkeys(ids) if known.get(key, 0) <= 0]
                if pairs:
                    end, _written = self._append(pairs)
                    self._install(conn, pairs)
                    self._journal_checkpoint(conn, end)
        if not complete:
            raise GuidanceIndexIncomplete('guidance delivered journal recovery is partial')

    # LLM: 队列仅定位，回执才决定状态；精确偏移复读可发现截断、重复编号和同号异文。
    # 函数用途: 安装一个队列位置，不从其正文推断幂等消息状态。
    def _queue_install(self, conn, entry, offset, length):
        key = str((entry.metadata or {}).get('dedupe_key') or '').strip()
        existing = conn.execute('SELECT * FROM entries WHERE id=?', (entry.guidance_id,)).fetchone()
        if existing and (existing['target_type'], existing['target_id'], existing['sequence']) != (entry.target_type, entry.target_id, offset):
            raise DataCorruptionError('guidance queue contains duplicate identity')
        conn.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?)',
                     (entry.guidance_id, entry.target_type, entry.target_id, offset, length, key))
        if key:
            conn.execute("INSERT OR IGNORE INTO pending VALUES (?,?,?,'unknown',?)",
                         (entry.guidance_id, entry.target_type, entry.target_id, offset))
            conn.execute('UPDATE pending SET sequence=? WHERE id=?', (offset, entry.guidance_id))
        else:
            at = conn.execute('SELECT at FROM acks WHERE id=?', (entry.guidance_id,)).fetchone()
            status = 'retired' if at and at[0] > 0 else 'pending'
            conn.execute('INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?)',
                         (entry.guidance_id, entry.target_type, entry.target_id, status, offset))

    # LLM: 持久字节游标每次最多64条；替换/缩短先清投影，append只读新增后缀，坏行保留load_errors。
    # 函数用途: 回填一个目标队列的有界切片。来源文件不存在时不保留来源行。
    def _queue_slice(self, conn, target_type, target_id, *, deadline=None):
        path = self.storage.guidance_path(target_type, target_id)
        identity = _identity(path)
        old = conn.execute('SELECT stamp,offset FROM sources WHERE path=?', (str(path),)).fetchone()
        previous = json.loads(old[0]) if old else None
        offset = int(old[1]) if old else 0
        stamp = json.dumps(identity)
        if old and old[0] == stamp and (identity is None or offset == identity[2]):
            return True
        append = identity and previous and list(identity[:2]) == previous[:2] and identity[2] > previous[2] and offset <= identity[2]
        if previous != (list(identity) if identity else None) and not append:
            conn.execute('DELETE FROM entries WHERE target_type=? AND target_id=?', (target_type, target_id))
            conn.execute('DELETE FROM pending WHERE target_type=? AND target_id=?', (target_type, target_id))
            conn.execute('DELETE FROM errors WHERE path=?', (str(path),))
            offset = 0
        if identity and offset != identity[2]:
            with _reader(path) as handle:
                handle.seek(offset)
                for _ in range(64):
                    check_io_deadline(deadline)
                    start = handle.tell()
                    raw = handle.readline(1048577)
                    if not raw:
                        break
                    if len(raw) > 1048576 or not raw.endswith(b'\n'):
                        raise DataCorruptionError('guidance queue has oversized or incomplete row')
                    if raw.strip():
                        try:
                            payload = json.loads(raw)
                            if not isinstance(payload, dict):
                                raise ValueError('guidance queue row must be an object')
                            entry = GuidanceEntry.from_dict(payload)
                            if entry.target_type != target_type or entry.target_id != target_id:
                                raise ValueError('guidance queue scope mismatch')
                            self._queue_install(conn, entry, start, len(raw))
                        except (ValueError, TypeError, KeyError) as exc:
                            conn.execute('INSERT OR REPLACE INTO errors VALUES (?,?,?)', (str(path), start, str(exc)))
                    offset = handle.tell()
            if _identity(path) != identity:
                raise GuidanceIndexIncomplete('guidance queue changed during indexing')
        if identity is None:
            if old:
                conn.execute('DELETE FROM sources WHERE path=?', (str(path),))
            return True
        conn.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)', (str(path), stamp, offset))
        return offset == identity[2]

    # LLM: 只有原receipt锁内的调用方能发布状态；读取候选仍复读回执，不把此表作为授权。
    # 函数用途: 从已提交规范回执维护目标/状态/次序投影。
    def publish(self, receipt, *, blocking=True, deadline=None):
        entry = receipt.entry
        with self._connection(blocking=blocking, deadline=deadline) as conn:
            row = conn.execute('SELECT sequence FROM entries WHERE id=?', (entry.guidance_id,)).fetchone()
            sequence = row[0] if row else -1
            previous = conn.execute('SELECT target_type,target_id,status,sequence FROM pending WHERE id=?', (entry.guidance_id,)).fetchone()
            if previous and tuple(previous) == (entry.target_type, entry.target_id, receipt.status, sequence):
                return
            conn.execute('INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?)',
                         (entry.guidance_id, entry.target_type, entry.target_id, receipt.status, sequence))

    # LLM: 意图不授权任何状态，只记需复读的canonical路径；先记意图再写canonical，成功后发布投影并清意图。
    # 函数用途: 返回一个回执的持久修复标记路径。
    def dirty_path(self, path):
        return self.dirty_dir / (hashlib.sha256(str(path).encode()).hexdigest() + '.json')

    # LLM: db锁不能包住receipt锁；始终先取原receipt锁后开投影事务，避免与写者交错死锁。
    #   逐标记隔离：失败标记保留并归因到回执目标（改绑只换回合、不换目标），标记或回执读不出时归所有目标；
    #   只在归因命中target或所有目标时重抛首个错误，target=None沿原语义任一失败即抛。
    # 函数用途: 每次最多修复64个跨库提交窗口。
    def _repair_dirty(self, *, target=None):
        if not self.dirty_dir.exists():
            return True
        with os.scandir(self.dirty_dir) as iterator:
            names = [item.path for item in itertools.islice(iterator, 65) if item.name.endswith('.json')]
        failures = []
        for name in names[:64]:
            marker = Path(name)
            receipt = None
            try:
                try:
                    with _reader(marker) as handle:
                        payload = json.loads(handle.read(65537))
                except FileNotFoundError:
                    continue
                if not isinstance(payload, dict) or not payload.get('receipt'):
                    raise DataCorruptionError('guidance repair marker is unreadable')
                path = Path(payload['receipt'])
                if path.parent != self.storage.guidance_dedupe_dir:
                    raise DataCorruptionError('guidance repair marker scope mismatch')
                with locked_file_transition(path.with_name(f'.{path.name}.transition')):
                    receipt = self.ledger.read_receipt(path)
                    if receipt is not None:
                        self.ledger.repair_projections(receipt)
                    marker.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, sqlite3.Error, DataCorruptionError) as exc:
                failures.append((None if receipt is None else (receipt.entry.target_type, receipt.entry.target_id), exc))
        for scope, exc in failures:
            if target is None or scope is None or scope == target:
                raise exc
        return len(names) <= 64

    # LLM: 一次调用只推进一个切片；未知回执在原锁内复读，缺失或损坏只报错，不重放提交未知消息。
    # 函数用途: 恢复一个目标的退休事实和队列状态，返回是否完整。
    def refresh(self, target_type, target_id):
        dirty_complete = self._repair_dirty(target=(target_type, target_id))
        with self._connection() as conn:
            ack_complete = self._prepare_acks(conn)
            queue_complete = self._queue_slice(conn, target_type, target_id)
            rows = conn.execute("SELECT e.* FROM entries e JOIN pending p ON e.id=p.id WHERE p.target_type=? AND p.target_id=? AND p.status='unknown' ORDER BY p.sequence LIMIT 64", (target_type, target_id)).fetchall()
        for row in rows:
            path = self.storage.guidance_dedupe_path(row['dedupe'])
            with locked_file_transition(path.with_name(f'.{path.name}.transition')):
                receipt = self.ledger.read_receipt(path)
                if receipt is None or receipt.entry.guidance_id != row['id']:
                    raise DataCorruptionError('guidance indexed receipt is missing or mismatched')
                self.publish(receipt)
        with self._connection() as conn:
            unknown = conn.execute("SELECT 1 FROM pending WHERE target_type=? AND target_id=? AND status='unknown' LIMIT 1", (target_type, target_id)).fetchone()
        return bool(dirty_complete and ack_complete and queue_complete and not unknown)

    # LLM: 原queue锁保留；先有界定位后精确复读，不能为去重扫描全queue；未追平时显式partial。
    # 函数用途: 在原队列中幂等追加一条规范entry。
    def ensure_queue_entry(self, entry, *, blocking=True, deadline=None):
        path = self.storage.guidance_path(entry.target_type, entry.target_id)
        ensure_private_dir(path.parent)
        complete = False
        with locked_json_path(path, blocking=blocking):
            check_io_deadline(deadline)
            with self._connection(blocking=blocking, deadline=deadline) as conn:
                complete = self._queue_slice(conn, entry.target_type, entry.target_id, deadline=deadline)
                if complete:
                    errors = conn.execute('SELECT 1 FROM errors WHERE path=? LIMIT 1', (str(path),)).fetchone()
                    if errors:
                        raise DataCorruptionError('guidance queue is unreadable')
                    row = conn.execute('SELECT * FROM entries WHERE id=?', (entry.guidance_id,)).fetchone()
                    if row:
                        existing = self.read_entry(row)
                        if not _guidance_queue_entry_matches_receipt(existing, entry):
                            raise DataCorruptionError('guidance queue identity conflicts')
                    else:
                        identity = _identity(path)
                        offset = identity[2] if identity else 0
                        check_io_deadline(deadline)
                        append_private_jsonl_records_unlocked(path, [entry.to_dict()], sort_keys=True, deadline=deadline)
                        identity = _identity(path)
                        self._queue_install(conn, entry, offset, identity[2] - offset)
                        conn.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)', (str(path), json.dumps(identity), identity[2]))
        if not complete:
            raise GuidanceIndexIncomplete('guidance queue backfill is partial')

    # LLM: 有限候选从JSONL精确位置取原字节；索引缓存不提供业务正文或权限。
    # 函数用途: 复读一条队列记录并核对身份与目标。
    def read_entry(self, row):
        path = self.storage.guidance_path(row['target_type'], row['target_id'])
        with _reader(path) as handle:
            handle.seek(row['sequence'])
            raw = handle.read(row['length'])
        try:
            entry = GuidanceEntry.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            raise DataCorruptionError('guidance indexed row is unreadable') from exc
        if (entry.guidance_id, entry.target_type, entry.target_id) != (row['id'], row['target_type'], row['target_id']):
            raise DataCorruptionError('guidance indexed row identity mismatch')
        return entry

    # LLM: 完整列表成本随实际返回规模；limit20只取最近匹配状态，历史终态不进入候选扫描。
    # 函数用途: 返回有界队列定位与累计坏行报告。
    def rows(self, target_type, target_id, limit, include_delivered):
        scope = (target_type, target_id, limit, include_delivered)
        signature = self._quiet_signature(scope)
        if self._quiet_hit(scope, signature):
            self.last_work = {'bytes_read': 0, 'bytes_written': 0, 'json_decodes': 0}
            return [], []

        queue_path = self.storage.guidance_path(target_type, target_id)
        # 纯读快速通道 1：索引与队列文件均不存在且无脏记录时，无需加写锁或初始化 SQLite，直接返回空
        dirty_has_entries = False
        if self.dirty_dir.exists():
            try:
                with os.scandir(self.dirty_dir) as it:
                    dirty_has_entries = any(True for _ in it)
            except OSError:
                dirty_has_entries = True

        if not self.path.exists() and not queue_path.exists() and not dirty_has_entries:
            self._remember_quiet(scope, signature, math.inf)
            self.last_work = {'bytes_read': 0, 'bytes_written': 0, 'json_decodes': 0}
            return [], []

        # 纯读快速通道 2：SQLite 索引已就绪且队列无未提交漂移时，通过只读连接查询，避免 exclusive flock
        if self.path.exists() and not dirty_has_entries:
            try:
                with self._read_connection(blocking=False, deadline=None) as conn:
                    if conn is not None and self._acks_current(conn):
                        queue_synced = False
                        if not queue_path.exists():
                            queue_synced = True
                        else:
                            row = conn.execute("SELECT stamp, offset FROM sources WHERE path=?", (str(queue_path),)).fetchone()
                            if row is not None and row["stamp"] == _identity(queue_path):
                                queue_synced = True
                        if queue_synced:
                            args = [target_type, target_id]
                            if include_delivered:
                                sql = 'SELECT * FROM entries WHERE target_type=? AND target_id=? ORDER BY sequence DESC'
                            else:
                                sql = "SELECT e.* FROM pending p JOIN entries e ON e.id=p.id WHERE p.target_type=? AND p.target_id=? AND p.status='pending' ORDER BY p.sequence DESC"
                            if limit > 0:
                                sql += ' LIMIT ?'
                                args.append(limit)
                            read_rows = conn.execute(sql, args).fetchall()
                            errors = [{'code': 'GUIDANCE_QUEUE_INVALID', 'context': 'conversation.guidance.read',
                                       'path': str(queue_path), 'offset': r[0], 'message': r[1]}
                                      for r in conn.execute('SELECT offset,error FROM errors WHERE path=? ORDER BY offset', (str(queue_path),))]
                            if not read_rows and not errors:
                                self._remember_quiet(scope, signature, math.inf)
                            else:
                                self._quiet = None
                            return list(reversed(read_rows)), errors
            except Exception:
                pass

        complete = self.refresh(target_type, target_id)
        if not complete:
            raise GuidanceIndexIncomplete('guidance index recovery is partial; retry continues its cursor')
        with self._connection() as conn:
            args = [target_type, target_id]
            if include_delivered:
                sql = 'SELECT * FROM entries WHERE target_type=? AND target_id=? ORDER BY sequence DESC'
            else:
                sql = "SELECT e.* FROM pending p JOIN entries e ON e.id=p.id WHERE p.target_type=? AND p.target_id=? AND p.status='pending' ORDER BY p.sequence DESC"
            if limit > 0:
                sql += ' LIMIT ?'
                args.append(limit)
            rows = conn.execute(sql, args).fetchall()
            path = self.storage.guidance_path(target_type, target_id)
            errors = [{'code': 'GUIDANCE_QUEUE_INVALID', 'context': 'conversation.guidance.read',
                       'path': str(path), 'offset': row[0], 'message': row[1]}
                      for row in conn.execute('SELECT offset,error FROM errors WHERE path=? ORDER BY offset', (str(path),))]
        if not rows and not errors:
            # 插话没有延迟 due 状态；任何新队列、退休事实或脏标记都会改变签名。
            self._remember_quiet(scope, signature, math.inf)
        else:
            self._quiet = None
        return list(reversed(rows)), errors

    def has_pending(self, target_type=None, target_id=None) -> bool:
        """Fast lock-free check for pending guidance entries."""
        if not self.path.exists():
            if target_type and target_id:
                queue_path = self.storage.guidance_path(target_type, target_id)
                if not queue_path.exists():
                    return False
            return False
        try:
            with self._read_connection(blocking=False, deadline=None) as conn:
                if conn is not None:
                    if target_type is not None and target_id is not None:
                        row = conn.execute(
                            "SELECT 1 FROM pending WHERE target_type=? AND target_id=? AND status='pending' LIMIT 1",
                            (target_type, target_id),
                        ).fetchone()
                    elif target_type is not None:
                        row = conn.execute(
                            "SELECT 1 FROM pending WHERE target_type=? AND status='pending' LIMIT 1",
                            (target_type,),
                        ).fetchone()
                    else:
                        row = conn.execute("SELECT 1 FROM pending WHERE status='pending' LIMIT 1").fetchone()
                    return row is not None
        except Exception:
            pass
        if target_type and target_id:
            rows, _ = self.rows(target_type, target_id, limit=1, include_delivered=False)
            return len(rows) > 0
        return False

    def list_pending(self, target_type=None, target_id=None, limit=20) -> list[dict]:
        """Fast query for pending guidance entries."""
        if not self.path.exists():
            return []
        try:
            with self._read_connection(blocking=False, deadline=None) as conn:
                if conn is not None:
                    args = []
                    where = "p.status='pending'"
                    if target_type is not None:
                        where += " AND p.target_type=?"
                        args.append(target_type)
                    if target_id is not None:
                        where += " AND p.target_id=?"
                        args.append(target_id)
                    sql = f"SELECT e.* FROM pending p JOIN entries e ON e.id=p.id WHERE {where} ORDER BY p.sequence DESC"
                    if limit > 0:
                        sql += " LIMIT ?"
                        args.append(limit)
                    return list(conn.execute(sql, args).fetchall())
        except Exception:
            pass
        if target_type and target_id:
            rows, _ = self.rows(target_type, target_id, limit=limit, include_delivered=False)
            return rows
        return []

    def _quiet_signature(self, scope):
        target_type, target_id, _limit, _include_delivered = scope
        return (_identity(self.journal_path), _identity(self.storage.guidance_delivered_path),
                _identity(self.storage.guidance_path(target_type, target_id)),
                _directory_stamp(self.dirty_dir))

    # LLM: 兼容显式全列表读口，不得在pending/recent或回执热路径调用。
    # 函数用途: 返回已经完整恢复的全部送达事实。
    def all_delivered(self, *, blocking=True, deadline=None):
        with self._read_connection(blocking=blocking, deadline=deadline) as conn:
            if conn is not None and self._acks_current(conn):
                self.last_work = {'bytes_read': 0, 'bytes_written': 0, 'json_decodes': 0}
                return dict(conn.execute('SELECT id,at FROM acks'))
        with self._connection(blocking=blocking, deadline=deadline) as conn:
            complete = self._prepare_acks(conn, blocking=blocking, deadline=deadline)
            result = dict(conn.execute('SELECT id,at FROM acks')) if complete else None
        if result is None:
            raise GuidanceIndexIncomplete('guidance delivered recovery is partial')
        return result
