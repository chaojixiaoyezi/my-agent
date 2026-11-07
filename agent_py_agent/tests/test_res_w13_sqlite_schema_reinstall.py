"""缓存 SQLite 对象在丢库后的真实重装、原件保护及权限边界回归。"""
from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agent_py_agent.agent.adapter.ingress import GatewayIngressRecord, GatewayIngressStore
from agent_py_agent.agent.adapter.ingress_index import IngressIndex
from agent_py_agent.agent.collaboration.history_index import CollaborationHistoryIndex
from agent_py_agent.agent.collaboration.models import AgentCapability
from agent_py_agent.agent.collaboration.store import CollaborationStore
from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.session_lock.store import SessionLockStore
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex


def _portable(monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs

    # 有些调用者按名导入；只切换能力探针，所有路径门、SQL 和写者仍是真实实现。
    for name, module in tuple(sys.modules.items()):
        if name.startswith('agent_py_agent.') and hasattr(module, '_supports_dir_fd'):
            monkeypatch.setattr(module, '_supports_dir_fd', lambda: False)
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)


def _record(number):
    identity = hashlib.sha256(f'res-w13-{number}'.encode()).hexdigest()
    return GatewayIngressRecord(ingress_id=identity, channel='feishu', user_id='fixture-user',
        conversation_id='fixture-chat', provider_message_id=f'message-{number}',
        canonical_payload_digest=identity, content=f'fixture-{number}',
        metadata={'owner_id': 'local/main'}, created_at=1.0, updated_at=1.0)


def _family(path):
    for suffix in ('', '-wal', '-shm', '-journal'):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _tables(path):
    with sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _build(kind, base):
    base.mkdir(parents=True, exist_ok=True)
    if kind == 'runtime_db':
        store = RuntimeRepository(base / 'runtime.db')

        def write(number):
            row = store.create_task(owner_id='local/main', title=f'task-{number}')
            assert store.get_task(row['task_id'])['title'] == f'task-{number}'

        return store, store.db_path, write
    if kind == 'local_store':
        store = LocalStore(base / 'local_store' / 'local.db')

        def write(number):
            row = store.log_record(source_type='fixture', source_id=str(number),
                title=f'title-{number}', content='body')
            assert store.get_record(row.id).title == f'title-{number}'

        return store, store.db_path, write
    if kind == 'session_lock':
        store = SessionLockStore(base / 'session_lock' / 'private_chat_locks.db')

        def write(number):
            store.record_activity('fixture-user', now=100.0 + number)
            assert store.last_activity('fixture-user') == 100.0 + number

        return store, Path(store._path), write
    if kind == 'subagent_run_index':
        root = base / 'subagents'
        root.mkdir()
        store = SubagentRunIndex(root)

        def write(number):
            identity = f'fixture-run-{number}'
            store.mark_pending(identity)
            assert store.pending_record(identity)[0] == identity

        return store, store.path, write
    if kind == 'collaboration_history':
        root = base / 'collaboration'
        (root / 'cases').mkdir(parents=True)
        store = CollaborationHistoryIndex(root)

        def write(number):
            identity = f'fixture-agent-{number}'
            store.upsert_agent(AgentCapability(agent_id=identity, role='worker'))
            assert identity in {row.agent_id for row in store.agents()}

        return store, store.path, write
    store = GatewayIngressStore(base / 'channel-delivery')

    def write(number):
        row, new, conflict = store.put_if_absent(_record(number))
        assert row.ingress_id == _record(number).ingress_id and new and not conflict

    return store, base / 'channel-delivery' / 'ingress' / 'index.sqlite3', write


@pytest.mark.parametrize('kind,fault,portable', [
    (kind, fault, portable)
    for kind in ('runtime_db', 'local_store', 'session_lock', 'subagent_run_index',
                 'collaboration_history', 'gateway_ingress')
    for fault in ('file', 'parent', 'empty')
    for portable in ((False, True) if kind in ('subagent_run_index', 'collaboration_history') else (False,))
    if not (kind == 'runtime_db' and fault == 'parent')
])
def test_cached_object_reinstalls_once_and_preserves_survivors(tmp_path, monkeypatch, caplog,
        kind, fault, portable):
    if portable:
        _portable(monkeypatch)
    owner = tmp_path / 'owner'
    store, path, write = _build(kind, owner)
    write(0)
    expected_tables = _tables(path)
    unrelated = owner / 'unrelated' / 'history.bin'
    unrelated.parent.mkdir()
    unrelated.write_bytes(b'\x00unrelated-history\xff')
    sibling = path.parent / 'sibling.keep'
    sibling.write_bytes(b'neighbor')
    caplog.set_level(logging.WARNING)
    caplog.clear()
    if fault == 'parent':
        shutil.rmtree(path.parent)
    else:
        _family(path)
        if fault == 'empty':
            path.touch(mode=0o600)
    write(1)
    write(2)
    assert _tables(path) == expected_tables
    _, fresh_path, warm_fresh = _build(kind, tmp_path / 'fresh-control')
    warm_fresh(0)
    assert _tables(path) == _tables(fresh_path)
    assert unrelated.read_bytes() == b'\x00unrelated-history\xff'
    if fault != 'parent':
        assert sibling.read_bytes() == b'neighbor'
    messages = [row.getMessage() for row in caplog.records
        if row.getMessage().startswith(f'SQLITE_SCHEMA_REINSTALLED store={kind} ')
        and f'path={path}' in row.getMessage()]
    assert len(messages) == 1, messages
    # 缓存实例没有被替换；旧密码/行内容不会从不存在的备份中伪造回来。
    assert store is not None
    if kind == 'runtime_db':
        with store.transaction() as db:
            assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    elif kind == 'local_store':
        assert store.get_record(store.make_record_id('fixture', '0')) is None
    elif kind == 'session_lock':
        assert store.password_hash('fixture-user') is None


@pytest.mark.parametrize('mode', ['read_only', 'existing_only'])
def test_runtime_observation_never_recreates_deleted_database(tmp_path, mode):
    path = tmp_path / 'runtime.db'
    writer = RuntimeRepository(path)
    writer.create_task(owner_id='local/main', title='before')
    observer = RuntimeRepository(path, **{mode: True})
    _family(path)
    for live in (observer, RuntimeRepository(path, **{mode: True})):
        with pytest.raises(sqlite3.OperationalError):
            live.get_task('missing')
        assert not path.exists()


def test_session_read_only_missing_database_never_creates_or_unlocks(tmp_path):
    path = tmp_path / 'session_lock' / 'private_chat_locks.db'
    writer = SessionLockStore(path)
    writer.set_password_hash('fixture-user', 'fixture-hash')
    observer = SessionLockStore(path, read_only=True)
    _family(path)
    for live in (observer, SessionLockStore(path, read_only=True)):
        with pytest.raises(sqlite3.OperationalError):
            live.password_hash('fixture-user')
        assert not path.exists()
    writer.record_activity('fixture-user', now=101.0)
    assert not writer.has_password('fixture-user')


def test_runtime_concurrent_reinstall_is_stamped_once(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.runtime_db import schema

    store = RuntimeRepository(tmp_path / 'runtime.db')
    store.create_task(owner_id='local/main', title='before')
    _family(store.db_path)
    stamps = []
    original = schema.stamp_schema

    def stamp(db, fingerprint):
        stamps.append(fingerprint)
        return original(db, fingerprint)

    monkeypatch.setattr(schema, 'stamp_schema', stamp)
    barrier = Barrier(2)

    def create(number):
        barrier.wait(timeout=5)
        return store.create_task(owner_id='local/main', title=str(number))['task_id']

    caplog.set_level(logging.WARNING)
    caplog.clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert len(set(results)) == 2 and len(stamps) == 1
    assert sum(row.getMessage().startswith('SQLITE_SCHEMA_REINSTALLED store=runtime_db ')
        for row in caplog.records) == 1


@pytest.mark.parametrize('portable', [False, True])
def test_run_projection_rebuilds_canonical_sources_before_cached_manager_creates(tmp_path, monkeypatch, portable):
    if portable:
        _portable(monkeypatch)
    manager = SubAgentManager(tmp_path / 'subagents', workspace_root=tmp_path)
    first = manager.create_run(goal='first fixture')
    index = manager.persistence.run_index
    _family(index.path)
    assert index.coverage()['complete'] is False
    with pytest.raises(RuntimeError, match='coverage is partial'):
        index.active_counts()
    report = manager.persistence.backfill_run_index()
    assert report['complete'] is True and not report['errors'], report
    assert first.id in index.known_ids((first.id,))
    second = manager.create_run(goal='second fixture')
    assert second.id != first.id
    assert manager.persistence.run_index is index


@pytest.mark.parametrize('portable', [False, True])
def test_collaboration_rebuild_preserves_canonical_case_and_legacy_roster(tmp_path, monkeypatch, portable):
    if portable:
        _portable(monkeypatch)
    store = CollaborationStore(tmp_path / 'collaboration')
    case = store.open_case({'thread_id': 'fixture-thread'})
    roster = b'{"fixture-agent":{"agent_id":"fixture-agent","role":"worker"}}'
    store.capabilities_path.write_bytes(roster)
    original_case = store._case_path(case.case_id).read_bytes()
    index = store.history_index
    _family(index.path)
    assert index.coverage()['complete'] is False
    report = store.backfill_history_index()
    if portable:
        # 续作裁决：索引便携重装成功，但严格canonical读取不能伪装成无目录FD时也完成。
        assert report['complete'] is False and report['loaded'] == 0, report
        assert report['remaining'] == 1 and len(report['errors']) == 1, report
        assert report['errors'][0]['error_type'] == 'NoFollowPathError', report
        assert report['errors'][0]['context'] == 'collaboration.history.migration', report
        assert index.coverage()['complete'] is False
        with pytest.raises(NoFollowPathError, match='strict no-follow opening is unavailable'):
            index.agents()
    else:
        assert report['complete'] is True and not report['errors'], report
        assert [row.agent_id for row in index.agents()] == ['fixture-agent']
    assert store.capabilities_path.read_bytes() == roster
    assert store._case_path(case.case_id).read_bytes() == original_case
    assert store.history_index is index


# LLM: 首次构造尊重调用方的legacy判断；同一对象丢库后必须重新承担来源回填责任。
# 函数用途: 区分首次初始化与删库重装的覆盖标记。
@pytest.mark.parametrize('legacy', [False, True])
def test_ingress_initial_legacy_flag_is_preserved_but_reinstall_requires_backfill(tmp_path, legacy):
    index = IngressIndex(tmp_path / 'ingress', legacy=legacy)
    assert index.get('legacy') == str(int(legacy))
    _family(index.path)
    assert index.get('legacy') == '1'
    assert index.get('legacy') == '1'


# LLM: 不以零字节替身覆盖所有缺header情形；旁表及其二进制原件必须保留。
# 函数用途: 有效非零SQLite替换后，缓存实例仍能安装本库schema且不动旁表。
@pytest.mark.parametrize('kind,portable', [
    (kind, portable) for kind in ('runtime_db', 'local_store', 'session_lock',
        'subagent_run_index', 'collaboration_history', 'gateway_ingress')
    for portable in ((False, True) if kind in ('subagent_run_index', 'collaboration_history') else (False,))
])
def test_nonempty_database_without_store_header_preserves_foreign_table(tmp_path, monkeypatch, caplog,
        kind, portable):
    if portable:
        _portable(monkeypatch)
    _, path, write = _build(kind, tmp_path / 'owner')
    write(0)
    tables = _tables(path)
    _family(path)
    db = sqlite3.connect(path)
    try:
        db.execute('CREATE TABLE survivor (payload BLOB)')
        db.execute('INSERT INTO survivor VALUES (?)', (b'\x00surviving-row\xff',))
        db.commit()
    finally:
        db.close()
    assert path.stat().st_size > 0
    caplog.set_level(logging.WARNING)
    caplog.clear()
    write(1)
    write(2)
    assert _tables(path) == tables | {'survivor'}
    db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)
    try:
        assert db.execute('SELECT payload FROM survivor').fetchall() == [(b'\x00surviving-row\xff',)]
    finally:
        db.close()
    assert sum(row.getMessage().startswith(f'SQLITE_SCHEMA_REINSTALLED store={kind} ')
        for row in caplog.records) == 1


# LLM: 恢复后第一入口直接BEGIN，且可先抛业务异常，不能依赖coverage预热来提交初始化。
# 函数用途: 验证schema元数据独立持久化、热读事务和partial安全语义。
@pytest.mark.parametrize('kind', ['subagent_run_index', 'collaboration_history'])
@pytest.mark.parametrize('portable', [False, True])
@pytest.mark.parametrize('survivors', [False, True])
@pytest.mark.parametrize('abort_first', [False, True])
def test_projection_reinstall_precedes_first_read_transaction(tmp_path, monkeypatch,
        kind, portable, survivors, abort_first):
    if portable:
        _portable(monkeypatch)
    index, path, _ = _build(kind, tmp_path / 'owner')
    if kind == 'subagent_run_index':
        read = index.active_counts
        source = path.parent / 'surviving-run'
    else:
        def read():
            return index.pending_rows(['fixture-agent'], 10)

        source = path.parent / 'cases' / 'surviving-case.json'
    expected = read()
    if survivors:
        if kind == 'subagent_run_index':
            source.mkdir()
        else:
            source.write_text('{}')
    _family(path)
    if abort_first:
        with pytest.raises(RuntimeError, match='injected business failure'):
            with index.connect() as db:
                db.execute("INSERT INTO meta VALUES ('aborted_business', '1')")
                raise RuntimeError('injected business failure')
    if survivors and kind == 'subagent_run_index':
        with pytest.raises(RuntimeError, match='coverage is partial'):
            read()
    elif survivors:
        _, errors = read()
        assert any(row['error_type'] == 'PartialCoverage' for row in errors), errors
    else:
        assert read() == expected
        assert read() == expected
    assert index.coverage()['complete'] is (not survivors)
    with index.connect() as db:
        assert db.execute("SELECT 1 FROM meta WHERE key='aborted_business'").fetchone() is None


# LLM: 用真实已安装库模拟写锁内复查命中，只有第一次指纹探针模拟竞争落后。
# 函数用途: 验证LocalStore不把初始化写事务泄漏给随后显式BEGIN的业务入口。
def test_local_schema_recheck_ends_initializer_transaction(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import schema

    store = LocalStore(tmp_path / 'local.db')
    conn = store._connect()
    original = schema.schema_is_current
    calls = []

    def current(db, fingerprint):
        calls.append(1)
        return len(calls) != 1 and original(db, fingerprint)

    monkeypatch.setattr(schema, 'schema_is_current', current)
    try:
        installed = store._ensure_schema(conn, schema._local_schema_fingerprint(store.enable_fts))
        assert not conn.in_transaction
        assert installed is False and len(calls) == 2
        conn.execute('BEGIN IMMEDIATE')
        conn.rollback()
    finally:
        conn.rollback()
        conn.close()


def test_ingress_maintenance_reprojects_and_canonical_record_still_deduplicates(tmp_path):
    store = GatewayIngressStore(tmp_path / 'channel-delivery')
    record = _record(0)
    assert store.put_if_absent(record)[1] is True
    index = store._index
    assert isinstance(index, IngressIndex)
    canonical = store._record_path(record.ingress_id)
    original = canonical.read_bytes()
    _family(index.path)
    store.maintenance()
    assert [row.ingress_id for row in store.pending()] == [record.ingress_id]
    row, new, conflict = store.put_if_absent(record)
    assert row == record and not new and not conflict
    assert canonical.read_bytes() == original and store._index is index
