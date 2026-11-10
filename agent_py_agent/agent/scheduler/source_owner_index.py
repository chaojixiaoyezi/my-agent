"""原 scheduler SQLite 内的来源路由投影；owner 账本始终是唯一订阅权威。"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager

from .event_sources import event_rule_views, normalize_event_schedule


class SourceOwnerIndexError(RuntimeError):
    pass


def create_source_owner_schema(conn):
    import uuid

    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_source_owners'").fetchone()
    epoch = conn.execute("SELECT value FROM scheduler_due_meta WHERE key='event_source_epoch_v1'").fetchone()
    if not existed or not epoch:
        conn.execute("DELETE FROM scheduler_due_meta WHERE key='event_source_discovery_v1' OR key LIKE 'event_sources_v1:%'")
        conn.execute("INSERT OR REPLACE INTO scheduler_due_meta(key,value,updated_at) VALUES ('event_source_epoch_v1',?,0)", (uuid.uuid4().hex,))
    conn.execute('''CREATE TABLE IF NOT EXISTS scheduler_source_owners (
        channel TEXT NOT NULL, app_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        sender_type TEXT NOT NULL, sender_id_type TEXT NOT NULL, sender_id TEXT NOT NULL,
        provider TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
        job_id TEXT NOT NULL, rule_id TEXT NOT NULL, generation INTEGER NOT NULL,
        PRIMARY KEY(channel,app_id,chat_id,sender_type,sender_id_type,sender_id,
                    provider,owner_kind,owner_id,job_id,rule_id,generation))''')
    conn.execute('''CREATE INDEX IF NOT EXISTS scheduler_source_owner_idx
        ON scheduler_source_owners(provider,owner_kind,owner_id)''')


def source_owner_rows(owner, store):
    result = []
    for job in (store.get('jobs') or {}).values():
        if not isinstance(job, dict) or job.get('status') != 'active':
            continue
        for view in event_rule_views(job):
            state = (job.get('event_source_state') or {}).get(str(view.get('rule_id') or '')) or {}
            if not view.get('enabled', True) or not state.get('active'):
                continue
            source = normalize_event_schedule(view['schedule'])['source']
            for sender in source['sender_ids']:
                result.append(tuple(source[key] for key in ('channel', 'app_id', 'chat_id', 'sender_type', 'sender_id_type'))
                    + (sender,) + tuple(owner[key] for key in ('provider', 'kind', 'id'))
                    + (job['job_id'], str(view.get('rule_id') or ''), state['generation']))
    return result


def publish_source_owner_rows(conn, rows):
    conn.executemany('INSERT OR IGNORE INTO scheduler_source_owners VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)


class SchedulerSourceOwnerIndex:
    def source_chat_present(self, event):
        with _source_connection(self) as conn:
            row = conn.execute('''SELECT 1 FROM scheduler_source_owners WHERE channel=? AND app_id=?
                AND chat_id=? AND sender_type='bot' LIMIT 1''',
                tuple(event[key] for key in ('channel', 'app_id', 'chat_id'))).fetchone()
        return row is not None

    def source_projection_epoch(self):
        with _source_connection(self) as conn:
            row = conn.execute("SELECT value FROM scheduler_due_meta WHERE key='event_source_epoch_v1'").fetchone()
        if not row or not isinstance(row[0], str) or not row[0]:
            raise SourceOwnerIndexError('EVENT_SOURCE_INDEX_EPOCH_UNKNOWN')
        return row[0]

    def source_discovery_ready(self):
        with _source_connection(self) as conn:
            row = conn.execute("SELECT value FROM scheduler_due_meta WHERE key='event_source_discovery_v1'").fetchone()
        return bool(row and row[0] == 'complete')

    def mark_source_discovery_complete(self, *, expected_epoch=None):
        from ..common.global_write_guard import require_global_writer

        require_global_writer('scheduler_source_discovery')
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            epoch = conn.execute("SELECT value FROM scheduler_due_meta WHERE key='event_source_epoch_v1'").fetchone()
            if not epoch or expected_epoch is not None and epoch[0] != expected_epoch:
                raise SourceOwnerIndexError('EVENT_SOURCE_INDEX_EPOCH_CHANGED')
            conn.execute("INSERT OR REPLACE INTO scheduler_due_meta(key,value,updated_at) VALUES ('event_source_discovery_v1','complete',0)")

    def source_owner_page(self, event, *, after=None):
        """一次只取一个精确来源的 owner；不按历史或整个 owner 树枚举。"""
        key = tuple(event[name] for name in ('channel', 'app_id', 'chat_id', 'sender_type', 'sender_id_type', 'sender_id'))
        clause, args = '', key
        if after is not None:
            clause = ' AND (provider,owner_kind,owner_id) > (?,?,?)'
            args += tuple(after)
        with _source_connection(self) as conn:
            row = conn.execute('''SELECT provider,owner_kind,owner_id FROM scheduler_source_owners
                WHERE channel=? AND app_id=? AND chat_id=? AND sender_type=? AND sender_id_type=? AND sender_id=?'''
                + clause + ' GROUP BY provider,owner_kind,owner_id ORDER BY provider,owner_kind,owner_id LIMIT 1', args).fetchone()
        return tuple(row) if row is not None else None

    def settle_source_owner(self, owner, store):
        """canonical 发表后或原 owner 锁内重建本 owner 投影，不推断新的权限。"""
        rows = source_owner_rows(owner, store)
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM scheduler_source_owners WHERE provider=? AND owner_kind=? AND owner_id=?',
                         tuple(owner[key] for key in ('provider', 'kind', 'id')))
            publish_source_owner_rows(conn, rows)
            conn.execute("INSERT OR REPLACE INTO scheduler_due_meta(key,value,updated_at) VALUES (?,'complete',0)",
                (_source_owner_key(tuple(owner[key] for key in ('provider', 'kind', 'id'))),))
            conn.commit()

    def source_rule_refs(self, event, owner):
        key = tuple(event[name] for name in ('channel', 'app_id', 'chat_id', 'sender_type', 'sender_id_type', 'sender_id'))
        with _source_connection(self) as conn:
            rows = conn.execute('''SELECT job_id,rule_id,generation FROM scheduler_source_owners
                WHERE channel=? AND app_id=? AND chat_id=? AND sender_type=? AND sender_id_type=? AND sender_id=?
                AND provider=? AND owner_kind=? AND owner_id=? ORDER BY job_id,rule_id,generation''', key + tuple(owner)).fetchall()
        return [tuple(row) for row in rows]

    def prepare_source_jobs(self, owner, jobs, *, job_ids=None):
        rows = source_job_rows(owner, jobs)
        selected = tuple(job_ids if job_ids is not None else (job['job_id'] for job in jobs if job is not None))
        if not selected:
            return False
        key = tuple(owner[name] for name in ('provider', 'kind', 'id'))
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM scheduler_source_owners WHERE provider=? AND owner_kind=? AND owner_id=? '
                + 'AND job_id IN (' + ','.join('?' for _ in selected) + ')', key + selected).fetchall()
            if {tuple(row) for row in current} == set(rows):
                return False
            publish_source_owner_rows(conn, rows)
        return True

    def settle_source_jobs(self, owner, job_ids, jobs):
        rows = source_job_rows(owner, jobs)
        key = tuple(owner[name] for name in ('provider', 'kind', 'id'))
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany('''DELETE FROM scheduler_source_owners
                WHERE provider=? AND owner_kind=? AND owner_id=? AND job_id=?''',
                [key + (job_id,) for job_id in job_ids])
            publish_source_owner_rows(conn, rows)

    def source_owner_ready(self, owner):
        with _source_connection(self) as conn:
            row = conn.execute('SELECT value FROM scheduler_due_meta WHERE key=?', (_source_owner_key(owner),)).fetchone()
        return bool(row and row[0] == 'complete')


def _source_owner_key(owner):
    import hashlib
    import json

    return 'event_sources_v1:' + hashlib.sha256(json.dumps(tuple(owner)).encode()).hexdigest()


@contextmanager
def _source_connection(index):
    try:
        with closing(index._connect()) as conn:
            yield conn
    except (OSError, sqlite3.Error) as exc:
        raise SourceOwnerIndexError('EVENT_SOURCE_INDEX_UNAVAILABLE') from exc


def source_job_rows(owner, jobs):
    return source_owner_rows(owner, {'jobs': {job['job_id']: job for job in jobs if job is not None}})
