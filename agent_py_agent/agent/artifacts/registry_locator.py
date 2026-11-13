"""原产物登记簿的派生定位索引；路径不是授权，读取仍重核原登记及运行归属。"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    _portable_directory,
    _supports_dir_fd,
    open_directory_beneath,
    open_private_lock_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
)
from ..io.sqlite_scope import connect_projection_sqlite, scoped_connection
from ..path_access_policy import agent_home_root_for_owner, owner_home_containing

_PARTS = ('data', 'artifacts', '.registry-locations.sqlite3')
_BUDGET = 2 * 1024 * 1024
_DISCOVERY_LIMIT = 4


@contextmanager
def connection(owner):
    path = Path(owner).joinpath(*_PARTS)
    def factory():
        db = connect_projection_sqlite(path, timeout=5)
        try:
            db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.execute('PRAGMA journal_size_limit=1048576')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('CREATE TABLE IF NOT EXISTS refs (artifact_id TEXT NOT NULL, root TEXT NOT NULL, '
                       'run_id TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(artifact_id,root))')
            db.execute('CREATE INDEX IF NOT EXISTS refs_task ON refs(task_id,artifact_id,root)')
            db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            return db
        except BaseException:
            db.close()
            raise
    def source_proof():
        info = Path(owner).stat()
        return info.st_dev, info.st_ino
    with locked_json_path(path):
        descriptor = open_private_lock_beneath(owner, _PARTS)
        os.close(descriptor)
        with scoped_connection(path, False, factory, rebuild_projection=source_proof) as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def _owner_for_root(root):
    agent = agent_home_root_for_owner(root)
    return owner_home_containing(root, agent) if agent is not None else None


def _pending_parts(artifact_id, root):
    key = hashlib.sha256(artifact_id.encode()).hexdigest()
    name = hashlib.sha256(str(root).encode()).hexdigest() + '.json'
    return ('data', 'artifacts', 'locator_pending', key[:2], key, name)


def prepare_record(root, record):
    """原登记前发表精确ID待索引位置；不证明登记完成，不授予内容权限。"""
    root = Path(root).absolute()
    owner = _owner_for_root(root)
    if owner is None or not record.run_id or not record.task_id:
        return
    data = json.dumps({'artifact_id': record.artifact_id, 'root': str(root.relative_to(owner)),
        'run_id': record.run_id, 'task_id': record.task_id}).encode()
    write_bytes_atomic_beneath(owner, _pending_parts(record.artifact_id, root), data)


def publish_record(root, record):
    """只发布定位副本；原registry是唯一登记权威。"""
    root = Path(root).absolute()
    owner = _owner_for_root(root)
    if owner is None or not record.run_id or not record.task_id:
        return
    with connection(owner) as db:
        _put(db, owner, root, record)
    parts = _pending_parts(record.artifact_id, root)
    try:
        unlink_file_beneath(owner, parts)
        if _supports_dir_fd():
            descriptor = open_directory_beneath(owner, parts[:-2])
            try:
                os.rmdir(parts[-2], dir_fd=descriptor)
            finally:
                os.close(descriptor)
        else:
            _portable_directory(owner, parts[:-1], create=False).rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        pass  # 目录中仍有同ID的其它根待核，不删除其pending。



def repair_record(root, record):
    owner = _owner_for_root(root)
    if owner is not None and read_bytes_beneath(owner, _pending_parts(record.artifact_id, root), max_bytes=16385) is not None:
        publish_record(root, record)

def _put(db, owner, root, record):
    relative = str(Path(root).relative_to(owner))
    previous = db.execute('SELECT run_id,task_id FROM refs WHERE artifact_id=? AND root=?',
        (record.artifact_id, relative)).fetchone()
    if previous == (record.run_id, record.task_id):
        return
    db.execute('INSERT INTO refs VALUES (?,?,?,?) ON CONFLICT(artifact_id,root) DO UPDATE SET '
               'run_id=excluded.run_id,task_id=excluded.task_id',
               (record.artifact_id, relative, record.run_id, record.task_id))
    db.execute("INSERT INTO meta VALUES ('revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")


def read_object(owner, path):
    raw = read_bytes_beneath(owner, Path(path).relative_to(owner).parts, max_bytes=_BUDGET + 1)
    if raw is None or len(raw) > _BUDGET:
        raise ValueError('ARTIFACT_LOCATOR_SOURCE_UNAVAILABLE')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('ARTIFACT_LOCATOR_SOURCE_INVALID')
    return value


def discover(owner, store, owner_id):
    """每次正式查询只补四个原run定位；持久keyset续页，不枚举运行目录。"""
    from ..subagents.services.persistence.run_index import SubagentRunIndex
    from .registry import REGISTRY_RELATIVE_PATH, _record_from_payload
    from .registry_reader import latest_records_from_lines

    owner = Path(owner).absolute()
    workspace = Path(store.storage.root).parent / 'subagents'
    index = SubagentRunIndex(workspace, read_only=True)
    errors, publications = [], []
    with connection(owner) as db:
        row = db.execute("SELECT value FROM meta WHERE key='discovery_after'").fetchone()
        saved = row[0] if row else ''
        prior_error = db.execute("SELECT value FROM meta WHERE key='discovery_errors'").fetchone()
        prior_error_count = int(prior_error[0]) if prior_error else 0
        after = int(saved) if saved.isdigit() else 0
    # 文件读取不持有locator写事务；多个reader竞争只会重复同一有界页，不会跳过尚未检查的根。
    with index.connect() as source:
        index._require_complete(source)
        info = index.path.stat()
        generation = f'{info.st_dev}:{info.st_ino}'
        with connection(owner) as db:
            prior = db.execute("SELECT value FROM meta WHERE key='discovery_source'").fetchone()
        high_water = source.execute('SELECT COALESCE(MAX(rowid),0) FROM runs').fetchone()[0]
        if prior is None or prior[0] != generation or after > high_water:
            after, prior_error_count = 0, 0
        rows = source.execute('SELECT rowid,run_id FROM runs WHERE rowid>? ORDER BY rowid LIMIT ?',
            (after, _DISCOVERY_LIMIT + 1)).fetchall()
    for _, run_id in rows[:_DISCOVERY_LIMIT]:
        try:
            state = read_object(owner, workspace / run_id / 'task.json')
            if state.get('canonical_state_ref'):
                state = read_object(owner, Path(state['canonical_state_ref']))
            if state.get('id') != run_id or state.get('owner') != owner_id:
                raise ValueError('ARTIFACT_LOCATOR_OWNER_RUN_MISMATCH')
            root = Path(state.get('agent_run_workspace_dir') or state.get('task_dir') or '')
            parts = root.relative_to(owner).parts
            raw = read_bytes_beneath(owner, (*parts, *REGISTRY_RELATIVE_PATH.parts), max_bytes=_BUDGET + 1)
            if raw is None:
                continue
            if len(raw) > _BUDGET:
                raise ValueError('ARTIFACT_REGISTRY_READ_BUDGET')
            records, bad = latest_records_from_lines(root / REGISTRY_RELATIVE_PATH,
                raw.decode('utf-8').splitlines(), _record_from_payload)
            if bad:
                raise ValueError('ARTIFACT_REGISTRY_UNREADABLE')
            publications.extend((root, record) for record in records.values() if record.run_id == run_id)
        except (OSError, ValueError, TypeError, KeyError) as error:
            errors.append(type(error).__name__)
    with connection(owner) as db:
        for root, record in publications:
            _put(db, owner, root, record)
        current = db.execute("SELECT value FROM meta WHERE key='discovery_after'").fetchone()
        if (current[0] if current else '') == saved:
            next_after = rows[min(_DISCOVERY_LIMIT, len(rows)) - 1][0] if rows else after
            db.execute("INSERT OR REPLACE INTO meta VALUES ('discovery_after',?)", (str(next_after),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('discovery_source',?)", (generation,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('discovery_errors',?)", (str(prior_error_count + len(errors)),))
    return {'coverage': 'partial' if len(rows) > _DISCOVERY_LIMIT else 'unknown' if prior_error_count + len(errors) else 'complete',
        'after': next_after if 'next_after' in locals() else after, 'source_generation': generation}


def lookup(owner, artifact_id):
    owner = Path(owner)
    with connection(owner) as db:
        rows = db.execute('SELECT root,run_id,task_id FROM refs WHERE artifact_id=? LIMIT 3', (artifact_id,)).fetchall()
    key = hashlib.sha256(artifact_id.encode()).hexdigest()
    parts = ('data', 'artifacts', 'locator_pending', key[:2], key)
    descriptor = None
    try:
        if _supports_dir_fd():
            descriptor = open_directory_beneath(owner, parts)
            pending_root = descriptor
        else:
            pending_root = _portable_directory(owner, parts, create=False)
    except FileNotFoundError:
        pending_root = None
    if pending_root is not None:
        try:
            with os.scandir(pending_root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 3:
                        raise ValueError('ARTIFACT_LOCATOR_PENDING_CONFLICT')
                    raw = read_bytes_beneath(owner, (*parts, entry.name), max_bytes=16385)
                    if raw is None or len(raw) > 16384:
                        raise ValueError('ARTIFACT_LOCATOR_PENDING_UNREADABLE')
                    item = json.loads(raw)
                    if (not isinstance(item, dict) or item.get('artifact_id') != artifact_id
                            or any(not isinstance(item.get(key), str) or not item[key] for key in ('root', 'run_id', 'task_id'))):
                        raise ValueError('ARTIFACT_LOCATOR_PENDING_ID_MISMATCH')
                    rows.append((item['root'], item['run_id'], item['task_id']))
        finally:
            if descriptor is not None:
                os.close(descriptor)
    rows = set(rows)
    if len(rows) > 1:
        raise ValueError('ARTIFACT_LOCATOR_ID_CONFLICT')
    return next(iter(rows)) if rows else None


def page(owner, *, after='', task_id=None, limit=50):
    with connection(owner) as db:
        if task_id is None:
            rows = db.execute('SELECT artifact_id,root,run_id,task_id FROM refs WHERE artifact_id>? '
                'ORDER BY artifact_id,root LIMIT ?', (after, limit + 1)).fetchall()
        else:
            rows = db.execute('SELECT artifact_id,root,run_id,task_id FROM refs WHERE task_id=? AND artifact_id>? '
                'ORDER BY artifact_id,root LIMIT ?', (task_id, after, limit + 1)).fetchall()
    return rows[:limit], rows[limit - 1][0] if len(rows) > limit else None


def revision(owner):
    with connection(owner) as db:
        row = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        info = Path(owner).joinpath(*_PARTS).stat()
    return f'{info.st_dev}:{info.st_ino}:{row[0] if row else "0"}'
