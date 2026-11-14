"""Gateway 延迟消息队列及 canonical 请求定位；旧历史按固定切片重建，未追平不猜测缺失。"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import ensure_private_dir
from ..conversation.message_scan import (
    TRANSCRIPT_MAX_SEGMENT_BYTES,
    TranscriptReader,
    transcript_manifest,
)
from ..conversation.models import MessageLogEntry
from ..io.sqlite_scope import scoped_connection, sqlite_connection_factory
from ..runtime_errors import DataCorruptionError
from ..turn_end import normalize_turn_end_reason


class HistoryIndexPending(DataCorruptionError):
    """本轮已推进有界重建，但还不能裁决请求是否存在。"""


class GatewayHistoryLedger:
    def __init__(self, store):
        self.store = store
        self.path = Path(store.storage.root) / '.gateway-history.sqlite3'

    @contextmanager
    def connection(self):
        ensure_private_dir(self.path.parent)
        with locked_json_path(self.path):
            if not self.path.exists():
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.close(fd)
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DataCorruptionError("GATEWAY_LEDGER_PATH_UNSAFE: expected private regular file")
            os.chmod(self.path, 0o600, follow_symlinks=False)
            def connect():
                conn = sqlite3.connect(self.path, timeout=5, factory=sqlite_connection_factory())
                try:
                    conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('PRAGMA journal_size_limit=1048576')
                    conn.execute('PRAGMA synchronous=FULL')
                    conn.executescript('''
                        CREATE TABLE IF NOT EXISTS coverage(thread TEXT PRIMARY KEY, identity TEXT, cursor INTEGER, stamp TEXT);
                        CREATE TABLE IF NOT EXISTS facts(thread TEXT, request TEXT, role TEXT, part TEXT,
                            attempt TEXT, reason TEXT, channel_message TEXT,
                            PRIMARY KEY(thread,request,role,part,attempt,reason,channel_message));
                        CREATE INDEX IF NOT EXISTS completed_lookup ON facts(thread,request,role,part,reason,attempt);
                        CREATE INDEX IF NOT EXISTS channel_lookup ON facts(thread,role,channel_message);
                        CREATE TABLE IF NOT EXISTS repairs(id INTEGER PRIMARY KEY, source TEXT UNIQUE,
                            thread TEXT NOT NULL, request TEXT NOT NULL, final INTEGER NOT NULL,
                            queued INTEGER NOT NULL, payload TEXT NOT NULL);
                        CREATE INDEX IF NOT EXISTS repair_order ON repairs(thread,queued,id);
                    ''')
                    return conn
                except BaseException:
                    conn.close()
                    raise
            with scoped_connection(self.path, False, connect) as conn:
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise

    def _snapshot(self, thread):
        path = self.store.storage.message_path(thread)
        if not path.exists() and transcript_manifest(path) is None:
            return 'missing', 0, 'missing'
        with TranscriptReader(path) as reader:
            stat = os.fstat(reader.hot.fileno())
            identity = json.dumps([stat.st_dev, stat.st_ino, reader.base,
                                   (reader.manifest or {}).get('chain_sha256', '')])
            return identity, reader.end, json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])

    def catch_up(self, conn, thread, *, progress=None):
        identity, end, stamp = self._snapshot(thread)
        removed = 0
        previous = conn.execute('SELECT identity,cursor,stamp FROM coverage WHERE thread=?', (thread,)).fetchone()
        valid = bool(previous and previous[0] == identity and previous[1] <= end
                     and (previous[1] != end or previous[2] == stamp))
        cursor = previous[1] if valid else 0
        if not valid:
            removed = conn.execute('DELETE FROM facts WHERE rowid IN (SELECT rowid FROM facts WHERE thread=? LIMIT 500)', (thread,)).rowcount
            if conn.execute('SELECT 1 FROM facts WHERE thread=? LIMIT 1', (thread,)).fetchone():
                conn.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?,?)', (thread, 'reset', 0, ''))
                if progress is not None:
                    progress.update(cursor_before=0, cursor_after=0, source_end=end, reset_removed=removed)
                return False
        cursor_before = cursor
        if cursor < end:
            rows = []
            start = cursor
            with TranscriptReader(self.store.storage.message_path(thread)) as reader:
                reader.seek(cursor)
                for _ in range(500):
                    remaining = min(end - cursor, 1024 * 1024 - (cursor - start))
                    if remaining <= 0:
                        break
                    raw = reader.readline(remaining + 1)
                    if len(raw) > remaining:
                        if cursor != start:
                            break
                        # 原canonical允许单条大消息；只对首条借用现有64MiB上限，随后结束此片。
                        reader.seek(cursor)
                        raw = reader.readline(TRANSCRIPT_MAX_SEGMENT_BYTES + 1)
                        if len(raw) > TRANSCRIPT_MAX_SEGMENT_BYTES:
                            raise HistoryIndexPending("GATEWAY_HISTORY_ROW_EXCEEDS_BUDGET: unchanged cursor, no append")
                    if not raw.endswith(b"\n"):
                        raise DataCorruptionError("GATEWAY_HISTORY_TRUNCATED: no complete canonical row")
                    cursor = reader.tell()
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        if reader.manifest is not None:
                            raise DataCorruptionError("GATEWAY_HISTORY_COLD_ROW_INVALID: index not complete") from None
                        logging.getLogger(__name__).warning("JSONL_ROW_SKIPPED context=gateway.dedupe offset=%s", cursor - len(raw))
                        continue
                    if not isinstance(value, dict):
                        if reader.manifest is not None:
                            raise DataCorruptionError("GATEWAY_HISTORY_COLD_ROW_INVALID: index not complete")
                        logging.getLogger(__name__).warning("JSONL_ROW_SKIPPED context=gateway.dedupe offset=%s", cursor - len(raw))
                        continue
                    rows.append(MessageLogEntry.from_dict(value))
            for row in rows:
                metadata = row.metadata if isinstance(row.metadata, dict) else {}
                conn.execute('INSERT OR IGNORE INTO facts VALUES(?,?,?,?,?,?,?)', (
                    thread, str(metadata.get('gateway_request_id') or ''), row.role,
                    str(metadata.get('assistant_part_id') or 'final').strip() or 'final',
                    str(metadata.get('gateway_execution_attempt_id') or ''),
                    normalize_turn_end_reason(metadata.get('turn_end_reason')), row.channel_message_id))
        current_identity, current_end, current_stamp = self._snapshot(thread)
        if identity != current_identity or current_end < end or (current_end == end and current_stamp != stamp):
            raise DataCorruptionError('GATEWAY_HISTORY_SOURCE_CHANGED: canonical changed during indexing')
        conn.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?,?)', (thread, identity, cursor, current_stamp))
        if progress is not None:
            progress.update(cursor_before=cursor_before, cursor_after=cursor, source_end=current_end, reset_removed=removed)
        return cursor == current_end

    def matches(self, conn, thread, *, role, request_id, channel_message_id,
                assistant_part_id, execution_attempt_id='', turn_end_reason=''):
        base = (thread, request_id, role)
        if role != 'assistant':
            if conn.execute('SELECT 1 FROM facts WHERE thread=? AND request=? AND role=? LIMIT 1', base).fetchone():
                return True
            return bool(channel_message_id and conn.execute(
                'SELECT 1 FROM facts WHERE thread=? AND role=? AND channel_message=? LIMIT 1',
                (thread, role, channel_message_id)).fetchone())
        # 每个分支使用精确复合索引，不随当前请求的历史尝试数线性扫描。
        completed_sql = "SELECT 1 FROM facts WHERE thread=? AND request=? AND role=? AND part='final' AND reason='completed'"
        completed_params = list(base)
        if assistant_part_id != 'final':
            completed_sql += ' AND attempt != ?'
            completed_params.append(execution_attempt_id)
        if conn.execute(completed_sql + ' LIMIT 1', completed_params).fetchone():
            return True
        query = 'SELECT 1 FROM facts WHERE thread=? AND request=? AND role=? AND part=?'
        params = [*base, assistant_part_id]
        if execution_attempt_id:
            query += " AND attempt IN ('',?)"
            params.append(execution_attempt_id)
        if assistant_part_id == 'final' and normalize_turn_end_reason(turn_end_reason) == 'completed':
            query += " AND reason IN ('','completed')"
        return conn.execute(query + ' LIMIT 1', params).fetchone() is not None

    def import_repair(self, conn, source, payload, queued):
        metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
        when = metadata.get('gateway_repair_queued_at_ns')
        conn.execute('INSERT OR IGNORE INTO repairs(source,thread,request,final,queued,payload) VALUES(?,?,?,?,?,?)', (
            source, str(payload.get('thread_id') or ''), str(metadata.get('gateway_request_id') or ''),
            int(str(metadata.get('assistant_part_id') or 'final') == 'final'),
            when if type(when) is int else queued, json.dumps(payload, ensure_ascii=False)))


# 旧平铺目录没有可持久化的分页索引；有限、可关闭的目录游标只负责迁移发现。
# 进程重启可重扫，已提交的队列按source去重；未遍历完整前不消费候选。
_DISCOVERY = OrderedDict()
_DISCOVERY_LOCK = threading.Lock()


def legacy_repair_page(directory, limit=1000):
    key = str(directory)
    stamp = directory.stat()
    identity = (stamp.st_dev, stamp.st_ino)
    now = time.monotonic()
    with _DISCOVERY_LOCK:
        for stale_key, item in list(_DISCOVERY.items()):
            if now - item[3] > 300 or (stale_key == key and item[0] != identity):
                item[1].close()
                del _DISCOVERY[stale_key]
        item = _DISCOVERY.pop(key, None)
        if item is None:
            while len(_DISCOVERY) >= 64:
                _, old = _DISCOVERY.popitem(last=False)
                old[1].close()
            stack = ExitStack()
            try:
                iterator = stack.enter_context(os.scandir(directory))
            except BaseException:
                stack.close()
                raise
            item = (identity, stack, iterator, now)
        rows = []
        try:
            for _ in range(limit):
                rows.append(next(item[2]))
        except StopIteration:
            item[1].close()
            return rows, True
        except BaseException:
            item[1].close()
            raise
        _DISCOVERY[key] = (item[0], item[1], item[2], now)
        return rows, False


def _close_discovery():
    with _DISCOVERY_LOCK:
        while _DISCOVERY:
            _, item = _DISCOVERY.popitem()
            item[1].close()


atexit.register(_close_discovery)
