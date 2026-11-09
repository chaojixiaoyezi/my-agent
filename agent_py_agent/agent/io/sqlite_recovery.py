"""非 runtime SQLite 的无损恢复：误报解锁，损坏保全，仅完整重建可发表。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from ..common import json_io
from ..runtime_db import salvage
from ..runtime_db.recovery_guard import runtime_db_generation_guard
from ..runtime_db.schema import runtime_db_identity
from .sqlite_probe import require_plain_sqlite_files, sqlite_connections_locked_out


def _inventory(conn):
    """完整读取全部普通表；任何未知/虚表/坏页均拒绝无损证明。"""
    schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    if any(row[3] and row[3].lstrip().upper().startswith('CREATE VIRTUAL TABLE') for row in schema):
        raise ValueError('sqlite_virtual_table_recovery_requires_operator')
    counts, digests = {}, {}
    for kind, name, _table, _sql in schema:
        if kind == 'table':
            quoted = '"' + name.replace('"', '""') + '"'
            # NOT INDEXED 避免损坏索引为 count(*) 提供不完整的业务行数。
            columns = conn.execute(f'PRAGMA table_info({quoted})').fetchall()
            names = [column[1] for column in columns]
            ordering = ','.join('"' + column.replace('"', '""') + '" COLLATE BINARY' for column in names)
            has_rowid = 'WITHOUT ROWID' not in str(_sql).upper() and not {'rowid', '_rowid_', 'oid'} & set(names)
            selection = 'rowid,*' if has_rowid else '*'
            order = 'rowid' if has_rowid else ordering
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute(f'SELECT {selection} FROM {quoted} NOT INDEXED ORDER BY {order}'):
                # sqlite原生类型的repr保留bytes/float/text边界；长度分帧防相邻行拼接歧义。
                encoded = repr(tuple(row)).encode('utf-8')
                digest.update(len(encoded).to_bytes(8, 'big'))
                digest.update(encoded)
                count += 1
            counts[name] = count
            digests[name] = digest.hexdigest()
    return schema, counts, digests


def _build_candidate(db, candidate):
    with salvage._recovery_connection(db) as source:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        schema, counts, digests = _inventory(source)
        versions = tuple(source.execute(f'PRAGMA {key}').fetchone()[0] for key in ('user_version', 'application_id'))
        descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(candidate)) as target:
            # iterdump 必须完整耗尽；禁止忽略错误或仅取可读前缀。
            for statement in source.iterdump():
                target.execute(statement)
            for key, value in zip(('user_version', 'application_id'), versions):
                target.execute(f'PRAGMA {key}={value}')
            if _inventory(target) != (schema, counts, digests):
                raise ValueError('sqlite_candidate_inventory_mismatch')
            if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or target.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('sqlite_candidate_integrity_unverified')
            target.commit()
        source.rollback()
    descriptor = os.open(candidate, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {'schema_objects': len(schema), 'table_rows': counts, 'table_digests': digests, 'complete': True}


def _discard_unpublished_candidate(db, state):
    """只退役原journal绑定、尚未发表的半成品；源与四文件备份始终不动。"""
    candidate = salvage._artifact(db, state, 'candidate')
    if not os.path.lexists(candidate):
        return
    if state.get('phase') not in {'backed_up', 'building_candidate'}:
        raise ValueError('sqlite_unpublished_candidate_phase_unknown')
    if salvage._source_signature(db) != state['source_signature']:
        raise ValueError('sqlite_source_changed_before_candidate_retry')
    with runtime_db_generation_guard(candidate, exclusive=True):
        require_plain_sqlite_files(candidate, beneath=db.parent)
        # 同线程、其它线程或非托管SQLite读取者均需先证明排空，不能先unlink再猜已关闭。
        with sqlite_connections_locked_out(candidate):
            for suffix in ('-wal', '-shm', '-journal', ''):
                Path(str(candidate) + suffix).unlink(missing_ok=True)
            json_io._sync_parent_directory(candidate)


def recover_sqlite_database(path: Path) -> dict:
    """按原代次非阻塞恢复；无法完整读取时永久保留源/四文件备份和操作说明。"""
    db, state = Path(path).absolute(), {}
    try:
        with runtime_db_generation_guard(db, exclusive=True):
            with json_io.locked_json_path(salvage._marker_lock(db), blocking=False):
                state = salvage._load_state(db)
                current_identity = runtime_db_identity(db)
                if state.get('corruption_confirmed') and current_identity not in (
                        tuple(state.get('identity', ())), tuple(state.get('candidate_identity', ()))):
                    raise ValueError('sqlite_source_generation_changed_requires_operator')
                if state.get('phase') in {'publishing', 'published', 'candidate_ready'}:
                    if current_identity == tuple(state.get('candidate_identity', ())):
                        if salvage._file_digest(db) != state['candidate_digest']:
                            raise ValueError('sqlite_published_candidate_changed')
                    return salvage._publish_candidate(db, state)
                require_plain_sqlite_files(db)
                with sqlite_connections_locked_out(db):
                    pass
                identity = runtime_db_identity(db)
                same_source = tuple(state.get('identity', ())) == identity
                if not same_source or not state.get('backup'):
                    token = uuid.uuid4().hex
                    state = {'identity': list(identity), 'backup': db.name + '.damaged-' + token,
                             'candidate': db.name + '.salvage-' + token,
                             'source_signature': salvage._source_signature(db)}
                    salvage._save_state(db, state, 'backing_up')
                    salvage._copy_backup(db, salvage._artifact(db, state, 'backup'))
                    salvage._save_state(db, state, 'backed_up')
                elif state.get('phase') == 'backing_up':
                    salvage._copy_backup(db, salvage._artifact(db, state, 'backup'))
                    salvage._save_state(db, state, 'backed_up')
                # 先保全四文件，后开SQLite验证，避免只读探测改变SHM后才复制证据。
                try:
                    with salvage._recovery_connection(db) as conn:
                        healthy = conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                except sqlite3.Error as error:
                    from .sqlite_connection import sqlite_error_details
                    if sqlite_error_details(error).get('sqlite_primary_code') not in (11, 26):
                        raise
                    healthy = False
                if healthy:
                    return salvage._complete_recovery(db, state, 'healthy',
                        preserve_evidence=bool(state.get('corruption_confirmed')))
                if state.get('phase') == 'requires_recovery' and same_source:
                    raise salvage._RecoveryStopped({**state, 'status': 'requires_recovery'})
                state['corruption_confirmed'] = True
                _discard_unpublished_candidate(db, state)
                salvage._save_state(db, state, 'building_candidate')
                try:
                    state['receipt'] = _build_candidate(db, salvage._artifact(db, state, 'candidate'))
                except (sqlite3.Error, ValueError) as error:
                    state.update(status='requires_recovery', reason=type(error).__name__,
                        next_action='保留原库及四文件备份；由操作者提供完整备份或离线修复后重新核验，禁止创建空库。')
                    salvage._save_state(db, state, 'requires_recovery')
                    raise salvage._RecoveryStopped(state) from error
                candidate = salvage._artifact(db, state, 'candidate')
                if salvage._source_signature(db) != state['source_signature']:
                    raise ValueError('sqlite_source_changed_during_recovery')
                state.update(candidate_identity=list(runtime_db_identity(candidate)), candidate_digest=salvage._file_digest(candidate))
                salvage._save_state(db, state, 'candidate_ready')
                return salvage._publish_candidate(db, state)
    except salvage._RecoveryStopped as stopped:
        return {**stopped.report, 'path': str(db)}
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        return {**salvage._recovery_failure(db, state, error),
                'next_action': '保留原库及四文件备份；先排除活动连接/热journal，再由操作者离线修复后重试，禁止创建空库。'}
