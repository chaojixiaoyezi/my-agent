"""观测与插话查询的无锁读口、稳态零写入和回放边界。"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import guidance_pending_index as guidance_module
from agent_py_agent.agent.conversation import observation_index as observation_module


def _index(store, kind):
    return store.observations._index if kind == 'observation' else store.guidance.ledger.pending_index()


def _query(index, kind):
    return index.pending(20) if kind == 'observation' else index.rows('thread', 'target', 20, False)[0]


def _ack(index, kind, ids):
    if kind == 'observation':
        index.ack(ids, 10)
    else:
        index.mark_delivered(ids, 10)


def _lookup(index, kind, ids):
    return index.handled(ids) if kind == 'observation' else index.delivered_many(ids)


def _count_locks(monkeypatch):
    calls = []
    original = observation_module.locked_json_path

    @contextmanager
    def counted(path, *args, **kwargs):
        calls.append(path)
        with original(path, *args, **kwargs) as result:
            yield result

    monkeypatch.setattr(observation_module, 'locked_json_path', counted)
    monkeypatch.setattr(guidance_module, 'locked_json_path', counted)
    return calls


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_second_empty_query_does_not_take_file_lock(tmp_path, monkeypatch, kind):
    store = ConversationStore(tmp_path / 'conversations')
    index = _index(store, kind)
    calls = _count_locks(monkeypatch)
    assert _query(index, kind) == []
    assert calls
    calls.clear()
    assert _query(index, kind) == []
    assert calls == []


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_fifty_steady_reads_and_source_probes_do_not_grow_wal(tmp_path, kind):
    store = ConversationStore(tmp_path / 'conversations')
    index = _index(store, kind)
    if kind == 'observation':
        for number in range(3):
            (store.storage.observations_dir / f'shard-{number}.jsonl').touch()
    _ack(index, kind, ['known'])
    assert _query(index, kind) == []
    assert _lookup(index, kind, ['known', 'missing']) == {'known': 10}
    # 保留一个普通连接，使真实 WAL 帧的增长可观察；产品仍自行打开和关闭每个连接。
    with sqlite3.connect(index.path) as keeper:
        keeper.execute('SELECT 1 FROM meta').fetchone()
        wal = Path(str(index.path) + '-wal')
        before = wal.stat().st_size
        for _ in range(50):
            assert _query(index, kind) == []
            assert _lookup(index, kind, ['known', 'missing']) == {'known': 10}
            if kind == 'observation':
                assert index.maintenance()['coverage'] == 'complete'
            else:
                assert index.refresh('thread', 'target')
                assert index.all_delivered() == {'known': 10}
        assert wal.stat().st_size == before


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_second_connection_on_same_inode_has_no_ddl(tmp_path, monkeypatch, kind):
    index = _index(ConversationStore(tmp_path / 'conversations'), kind)
    assert _query(index, kind) == []
    original = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', traced)
    before = index.path.stat().st_ino
    with index._connection() as conn:
        assert conn.execute('PRAGMA synchronous').fetchone()[0] == 2
    assert index.path.stat().st_ino == before
    assert not any(sql.lstrip().upper().startswith(('CREATE ', 'DROP ')) for sql in statements)
    assert not any('JOURNAL_MODE=' in sql.upper() for sql in statements)


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_sixteen_readers_finish_while_writer_holds_original_lock(tmp_path, monkeypatch, kind):
    index = _index(ConversationStore(tmp_path / 'conversations'), kind)
    _ack(index, kind, ['known'])
    assert _query(index, kind) == []
    assert _lookup(index, kind, ['known', 'missing']) == {'known': 10}
    held, release, start, writer_finished = (threading.Event() for _ in range(4))
    statements = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        if '?mode=ro' in str(args[0]):
            conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', traced)

    def writer():
        try:
            with index._connection():
                held.set()
                assert release.wait(timeout=15)
        finally:
            writer_finished.set()

    def reader():
        assert start.wait(timeout=5)
        for _ in range(50):
            assert _lookup(index, kind, ['known', 'missing']) == {'known': 10}
            if kind == 'guidance':
                assert index.all_delivered() == {'known': 10}

    with ThreadPoolExecutor(max_workers=17) as pool:
        writing = pool.submit(writer)
        try:
            assert held.wait(timeout=5)
            reading = [pool.submit(reader) for _ in range(16)]
            start.set()
            for future in reading:
                future.result(timeout=10)
            assert not writer_finished.is_set()
        finally:
            release.set()
            writing.result(timeout=5)
    assert statements and 'BEGIN' in statements
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'BEGIN IMMEDIATE'))
                   for sql in statements)


def _clock(monkeypatch):
    clock = {'monotonic': 100.0, 'time': 1000.0}
    monkeypatch.setattr(observation_module, 'time', SimpleNamespace(
        monotonic=lambda: clock['monotonic'], time=lambda: clock['time']))
    return clock


def test_new_guidance_is_visible_before_quiet_window_expires(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    index = _index(store, 'guidance')
    clock = _clock(monkeypatch)
    assert _query(index, 'guidance') == []
    start, published = threading.Event(), threading.Event()

    def append():
        assert start.wait(timeout=5)
        entry = store.guidance.append({'target_type': 'thread', 'target_id': 'target', 'message': 'new'})
        published.set()
        return entry

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(append)
        start.set()
        assert published.wait(timeout=5)
        entry = writing.result(timeout=5)
    clock['monotonic'] += CACHE_TRUST_AGE_SECONDS / 2
    assert [row['id'] for row in _query(index, 'guidance')] == [entry.guidance_id]


def test_guidance_arriving_during_empty_result_publication_is_not_memoized(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    index = _index(store, 'guidance')
    start, published = threading.Event(), threading.Event()
    original = index._remember_quiet

    def remember(*args):
        start.set()
        assert published.wait(timeout=5)
        original(*args)

    def append():
        assert start.wait(timeout=5)
        entry = store.guidance.append({'target_type': 'thread', 'target_id': 'target', 'message': 'new'})
        published.set()
        return entry

    monkeypatch.setattr(index, '_remember_quiet', remember)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(append)
        assert _query(index, 'guidance') == []
        entry = writing.result(timeout=5)
    assert [row['id'] for row in _query(index, 'guidance')] == [entry.guidance_id]


def test_existing_observation_append_is_visible_at_quiet_expiry(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'u', 'channel': 'internal',
        'channel_conversation_id': 'c', 'channel_user_id': 'u', 'now': 1}).thread_id
    old = store.observations.append({'thread_id': thread, 'requires_main_agent': True})
    index = _index(store, 'observation')
    index.ack([old.observation_id], 10)
    clock = _clock(monkeypatch)
    assert _query(index, 'observation') == []
    start, published = threading.Event(), threading.Event()

    def append():
        assert start.wait(timeout=5)
        entry = store.observations.append({'thread_id': thread, 'requires_main_agent': True})
        published.set()
        return entry

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(append)
        start.set()
        assert published.wait(timeout=5)
        entry = writing.result(timeout=5)
    clock['monotonic'] += CACHE_TRUST_AGE_SECONDS
    assert [event.observation_id for event in _query(index, 'observation')] == [entry.observation_id]


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_ack_never_reappears_after_late_read_or_projection_rebuild(tmp_path, kind):
    store = ConversationStore(tmp_path / 'conversations')
    index = _index(store, kind)
    if kind == 'observation':
        thread = store.threads.get_or_create({'canonical_user_id': 'u', 'channel': 'internal',
            'channel_conversation_id': 'c', 'channel_user_id': 'u', 'now': 1}).thread_id
        key = store.observations.append({'thread_id': thread, 'requires_main_agent': True}).observation_id
    else:
        key = store.guidance.append({'target_type': 'thread', 'target_id': 'target', 'message': 'one'}).guidance_id
    assert len(_query(index, kind)) == 1
    start, acknowledged = threading.Event(), threading.Event()

    def ack():
        assert start.wait(timeout=5)
        _ack(index, kind, [key])
        acknowledged.set()

    def late_read():
        assert acknowledged.wait(timeout=5)
        assert _query(index, kind) == []

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing, reading = pool.submit(ack), pool.submit(late_read)
        start.set()
        writing.result(timeout=5)
        reading.result(timeout=5)
    index.path.unlink()
    assert _query(index, kind) == []
    restarted = _index(ConversationStore(store.storage.root), kind)
    assert _query(restarted, kind) == []
    assert _lookup(restarted, kind, [key]) == {key: 10}


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_unprojected_ack_replays_before_answering(tmp_path, monkeypatch, kind):
    index = _index(ConversationStore(tmp_path / 'conversations'), kind)
    _ack(index, kind, ['known'])
    assert _query(index, kind) == []
    calls = _count_locks(monkeypatch)
    with sqlite3.connect(index.path) as keeper:
        keeper.execute('SELECT 1 FROM meta').fetchone()
        index._append([('late', 20)])
        assert _lookup(index, kind, ['late']) == {'late': 20}
    assert calls


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_changed_legacy_fact_invalidates_quiet_and_read_snapshot(tmp_path, kind):
    store = ConversationStore(tmp_path / 'conversations')
    index = _index(store, kind)
    _ack(index, kind, ['known'])
    assert _query(index, kind) == []
    path = (store.storage.observation_handled_path if kind == 'observation'
            else store.storage.guidance_delivered_path)
    path.write_text('{"legacy": 30}', encoding='utf-8')
    with sqlite3.connect(index.path) as keeper:
        keeper.execute('SELECT 1 FROM meta').fetchone()
        assert _lookup(index, kind, ['legacy']) == {'legacy': 30}
    assert _query(index, kind) == []


@pytest.mark.parametrize('kind', ['observation', 'guidance'])
def test_read_deadline_still_applies_and_closes_connection(tmp_path, monkeypatch, kind):
    index = _index(ConversationStore(tmp_path / 'conversations'), kind)
    assert _query(index, kind) == []
    read_finished = threading.Event()

    def deadline(_deadline):
        if read_finished.is_set():
            raise TimeoutError('read deadline expired')

    monkeypatch.setattr(observation_module, 'check_io_deadline', deadline)
    with sqlite3.connect(index.path) as keeper:
        keeper.execute('SELECT 1 FROM meta').fetchone()
        with pytest.raises(TimeoutError, match='read deadline expired'):
            with index._read_connection() as conn:
                assert conn is not None
                conn.execute('SELECT 1 FROM meta').fetchone()
                read_finished.set()
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')


def test_unchanged_receipt_publication_does_not_grow_wal(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    store.guidance.append_once({'target_type': 'thread', 'target_id': 'target', 'message': 'same'},
                               dedupe_key='same')
    receipt = store.guidance.receipt('same')
    index = _index(store, 'guidance')
    with sqlite3.connect(index.path) as keeper:
        keeper.execute('SELECT 1 FROM meta').fetchone()
        wal = Path(str(index.path) + '-wal')
        before = wal.stat().st_size
        for _ in range(50):
            index.publish(receipt)
        assert wal.stat().st_size == before


@pytest.mark.parametrize('errors', [[], [{'code': 'GUIDANCE_QUEUE_INVALID'}]])
def test_empty_recent_report_preserves_errors_without_ack_query(tmp_path, monkeypatch, errors):
    store = ConversationStore(tmp_path / 'conversations')
    index = _index(store, 'guidance')
    monkeypatch.setattr(index, 'rows', lambda *args: ([], errors))

    def forbidden(*args, **kwargs):
        pytest.fail('空候选不应再打开回执索引')

    monkeypatch.setattr(index, 'delivered_many', forbidden)
    assert store.guidance.recent_report('thread', 'target') == ([], errors)
