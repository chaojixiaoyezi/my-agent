"""已消费本人输入的公开回复欠账；只保存身份，不复制正文或凭据。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing

from ..common.nofollow_fs import open_private_lock_beneath
from ..runtime_errors import DataCorruptionError

# 每次公开答复最多固定这一页义务，后续输入沿FIFO等待下一次交付。
PENDING_REPLY_PAGE_SIZE = 64


def direct_user_input(entry) -> bool:
    metadata = getattr(entry, 'metadata', None) or {}
    return metadata.get('kind') == 'active_turn_user_input' and not metadata.get('origin_kind')


def _connection(storage):
    path = storage.guidance_reply_obligations_path
    descriptor = open_private_lock_beneath(storage.root, (path.name,))
    os.close(descriptor)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute('PRAGMA auto_vacuum = INCREMENTAL')
        connection.execute('PRAGMA journal_size_limit = 1048576')
        connection.execute('CREATE TABLE IF NOT EXISTS replies ('
                           'input_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, task_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, '
                           'delivery_committed INTEGER NOT NULL DEFAULT 0, CHECK(length(input_id)>0))')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(replies)')}
        if 'delivery_committed' not in columns:
            connection.execute('ALTER TABLE replies ADD COLUMN delivery_committed INTEGER NOT NULL DEFAULT 0')
        connection.execute('CREATE INDEX IF NOT EXISTS pending_reply_page_scope '
                           'ON replies(thread_id, task_id, delivery_committed)')
        connection.execute('DROP INDEX IF EXISTS pending_reply_scope')
    except BaseException:
        connection.close()
        raise
    return connection


def register(storage, entries, *, task_id='', delivered=False):
    if delivered:
        return
    rows = []
    for entry in entries:
        if not direct_user_input(entry):
            continue
        metadata = entry.metadata
        thread = str(metadata.get('thread_id') or '')
        task = str(task_id or (entry.target_id if entry.target_type == 'task' else ''))
        if thread and task:
            rows.append((entry.guidance_id, thread, task, str(metadata.get("dedupe_key") or "")))
    if rows:
        with closing(_connection(storage)) as connection, connection:
            for row in rows:
                existing = connection.execute('SELECT thread_id, task_id, dedupe_key FROM replies WHERE input_id=?',
                                              (row[0],)).fetchone()
                if existing is not None and existing != row[1:]:
                    raise DataCorruptionError('USER_REPLY_SCOPE_CONFLICT: ACK committed; input scope changed; inspect the original receipt before retry')
            connection.executemany('INSERT OR IGNORE INTO replies(input_id, thread_id, task_id, dedupe_key) VALUES(?,?,?,?)', rows)


def pending(storage, thread_id, task_id, *, limit=PENDING_REPLY_PAGE_SIZE) -> tuple[str, ...]:
    path = storage.guidance_reply_obligations_path
    if not path.exists():
        return ()
    page_size = max(1, min(int(limit), PENDING_REPLY_PAGE_SIZE))
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        rows = tuple(connection.execute(
            'SELECT input_id, dedupe_key FROM replies WHERE thread_id=? AND task_id=? AND delivery_committed=0 ORDER BY rowid LIMIT ?',
            (thread_id, task_id, page_size)))
    from .store_guidance_ledger import GuidanceLedger

    ledger = GuidanceLedger(storage)
    result = []
    for identity, key in rows:
        receipt = ledger.read_receipt(storage.guidance_dedupe_path(key))
        if receipt is None or receipt.entry.guidance_id != identity:
            raise DataCorruptionError('USER_REPLY_RECEIPT_MISSING: delivery not started; pending input has no matching receipt; repair the original ACK before retry')
        # ACK批次已提交但逐条投影尚未完成时，不得提前授权公开回复。
        if receipt.status == 'consumed' and not receipt.reply_delivered:
            result.append(identity)
    return tuple(result)


def settle(storage, thread_id, task_id, input_ids):
    if not input_ids:
        return
    from dataclasses import replace

    from ..gateway_parts.io import locked_file_transition, write_json_file_atomic
    from .store_guidance_ledger import GuidanceLedger

    ledger = GuidanceLedger(storage)
    with closing(_connection(storage)) as connection:
        for identity in input_ids:
            row = connection.execute('SELECT dedupe_key FROM replies WHERE input_id=? AND thread_id=? AND task_id=?',
                                     (identity, thread_id, task_id)).fetchone()
            if row is None:
                continue
            # 外发/权威交付已经完成，先记录单调事实，补原receipt失败也不能让该行阻塞下一页。
            with connection:
                connection.execute('UPDATE replies SET delivery_committed=1 WHERE input_id=? AND thread_id=? AND task_id=?',
                                   (identity, thread_id, task_id))
            path = storage.guidance_dedupe_path(row[0])
            with locked_file_transition(path.with_name(f'.{path.name}.transition')):
                receipt = ledger.read_receipt(path)
                if receipt is None or receipt.entry.guidance_id != identity or receipt.status != 'consumed':
                    raise DataCorruptionError('USER_REPLY_RECEIPT_CONFLICT: delivery already committed; consumed input identity changed; repair only the original receipt, do not resend')
                ledger.write_receipt(path, replace(receipt, reply_delivered=True).to_dict(), writer=write_json_file_atomic)
            with connection:
                connection.execute('DELETE FROM replies WHERE input_id=? AND thread_id=? AND task_id=?',
                                   (identity, thread_id, task_id))
        connection.execute('PRAGMA incremental_vacuum(8)')
