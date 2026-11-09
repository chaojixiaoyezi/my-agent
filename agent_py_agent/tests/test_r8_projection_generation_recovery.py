"""可重建定位库共享代次锁：先保全，再按原canonical游标追平，未知来源不可当空。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_list_index import SessionListIndex
from agent_py_agent.agent.conversation.usage_index import UsageIndex
from agent_py_agent.agent.runtime_db.recovery_guard import (
    RuntimeDatabaseDeferred,
    close_runtime_connection,
    guarded_runtime_connect,
)


def _case(tmp_path, kind):
    store = ConversationStore(tmp_path / 'conversations')
    if kind == 'observation':
        index = store.observations._index
        index.ack(['known'], 10)
    elif kind == 'guidance':
        index = store.guidance.ledger.pending_index()
        index.mark_delivered(['known'], 10)
    elif kind == 'usage':
        source = tmp_path / 'usage.jsonl'
        source.touch()
        index = UsageIndex(source, 'thread')
    else:
        root = tmp_path / 'threads'
        root.mkdir()
        index = SessionListIndex(root, kind='thread', reader=lambda path: ({}, None))
    with index._connection() as conn:
        conn.execute('SELECT 1').fetchone()
    return index


@pytest.mark.parametrize('kind', ['observation', 'guidance', 'usage', 'list'])
def test_projection_corruption_preserves_bytes_then_replays_original_sources(tmp_path, kind):
    index = _case(tmp_path, kind)
    damage = b'not a sqlite database with evidence'
    index.path.write_bytes(damage)
    with index._connection() as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    result = json.loads(Path(str(index.path) + '.recovery-result.json').read_text())
    assert result['receipt'] == {'projection_rebuild': True, 'canonical_replay_required': True}
    assert (index.path.parent / result['backup']).read_bytes() == damage
    assert index.path.stat().st_ino != result['identity'][1]
    if kind == 'observation':
        assert index.handled(['known']) == {'known': 10}
    elif kind == 'guidance':
        assert index.delivered_many(['known']) == {'known': 10}
    elif kind == 'usage':
        with index._connection() as conn:
            assert index.prepare(conn)


def test_live_physical_shared_lease_fences_projection_replacement(tmp_path):
    index = _case(tmp_path, 'observation')
    damage = b'not sqlite and must not be truncated'
    index.path.write_bytes(damage)
    entered, release = threading.Event(), threading.Event()
    errors = []
    def reader():
        conn = None
        try:
            conn = guarded_runtime_connect(index.path, lambda: sqlite3.connect(index.path))
            entered.set()
            assert release.wait(5)
        except BaseException as error:
            errors.append(error)
        finally:
            if conn is not None:
                close_runtime_connection(conn)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeDatabaseDeferred):
            with index._connection():
                pytest.fail('active generation was replaced')
        assert index.path.read_bytes() == damage
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == []
    assert index.handled(['known']) == {'known': 10}


def test_missing_canonical_usage_source_blocks_empty_complete_rebuild(tmp_path):
    index = _case(tmp_path, 'usage')
    index.source.unlink()
    damage = b'not sqlite'
    index.path.write_bytes(damage)
    with pytest.raises(RuntimeDatabaseDeferred) as caught:
        with index._connection():
            pytest.fail('missing source cannot prove empty canonical history')
    assert index.path.read_bytes() == damage
    assert 'canonical_sources_unverified' in str(caught.value)
    assert any(path.read_bytes() == damage for path in tmp_path.glob('*.damaged-*'))


def test_unreadable_canonical_usage_is_not_complete_after_projection_rebuild(tmp_path):
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    index = _case(tmp_path, 'usage')
    index.source.write_text('not json\n')
    index.path.write_bytes(b'not sqlite')
    with index._connection() as conn:
        with pytest.raises(DataCorruptionError):
            index.prepare(conn)


def test_projection_publication_crash_rechecks_canonical_source_before_resume(tmp_path, monkeypatch):
    from agent_py_agent.agent.io import sqlite_recovery
    from agent_py_agent.agent.runtime_db import salvage

    index = _case(tmp_path, 'usage')
    index.path.write_bytes(b'damaged projection')
    original = salvage._publish_candidate
    def interrupted(db, state):
        raise KeyboardInterrupt('before projection publication')
    monkeypatch.setattr(salvage, '_publish_candidate', interrupted)
    with pytest.raises(KeyboardInterrupt):
        with index._connection():
            pytest.fail('publication must be interrupted')
    index.source.unlink()
    monkeypatch.setattr(salvage, '_publish_candidate', original)
    report = sqlite_recovery.recover_sqlite_database(index.path, automatic=True,
        rebuild_projection=index._projection_rebuild_proof)
    assert report['status'] == 'requires_recovery'
    assert 'canonical_sources_unverified' in report['reason']
    assert index.path.read_bytes() == b'damaged projection'


def test_projection_preserves_original_db_wal_shm_and_cold_journal(tmp_path):
    index = _case(tmp_path, 'observation')
    files = {'': b'original invalid database', '-wal': b'original wal',
             '-shm': b'original shm', '-journal': b'\0' * 8 + b'original journal'}
    for suffix, raw in files.items():
        Path(str(index.path) + suffix).write_bytes(raw)
    with index._connection():
        pass
    result = json.loads(Path(str(index.path) + '.recovery-result.json').read_text())
    backup = index.path.parent / result['backup']
    assert {suffix: Path(str(backup) + suffix).read_bytes() for suffix in files} == files
    assert index.handled(['known']) == {'known': 10}


def test_unknown_factory_error_never_resets_list_projection(tmp_path, monkeypatch):
    index = _case(tmp_path, 'list')
    original = index.path.read_bytes()
    def fail():
        raise sqlite3.OperationalError('unknown schema or environmental error')
    monkeypatch.setattr(index, '_open_projection', fail)
    with pytest.raises(sqlite3.OperationalError):
        with index._connection():
            pytest.fail('factory failure must propagate')
    assert index.path.read_bytes() == original
    assert not Path(str(index.path) + '.recovery-result.json').exists()
