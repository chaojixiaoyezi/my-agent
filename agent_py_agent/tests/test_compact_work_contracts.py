"""批次连接的线程/异常边界、精确索引比较、冻结原生回放等值性。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from agent_py_agent.agent.conversation.history_index import ensure_thread_history_indexed
from agent_py_agent.agent.conversation.message_replay import MessageRowAddress, MessageSnapshotRows
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows
from agent_py_agent.agent.conversation.native_history_replay import prepare_native_history_replay
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage.records import LocalRecordInput
from agent_py_agent.tests.test_compact_work_measurement import synthetic_case


def _check_nested_connection(store, first):
    with store.connection_batch(), store._connection() as nested:
        assert nested is first


def _check_other_thread_connection(store, first):
    with store.connection_batch(), store._connection() as second:
        assert second is not first
        second.execute('SELECT 1').fetchone()


def _raise_in_batch(store, connections):
    with store.connection_batch(), store._connection() as first:
        connections.append(first)
        _check_nested_connection(store, first)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_check_other_thread_connection, store, first).result()
        raise RuntimeError('test failure')


def test_connection_batch_nesting_exception_and_thread_isolation(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    connections = []
    with pytest.raises(RuntimeError, match='test failure'):
        _raise_in_batch(store, connections)
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute('SELECT 1')
    with store._connection() as fresh:
        assert fresh is not connections[0]
        fresh.execute('SELECT 1').fetchone()


def test_record_matches_checks_actual_body_and_metadata(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    record = LocalRecordInput('test', 'id', 'title', '中文\n' * 30000, {'x': [1, 2]}, record_id='id')
    assert not store.record_matches(record)
    stored = store.upsert_record(record)
    assert store.record_matches(record)
    assert not store.record_matches(replace(record, content=record.content + 'changed'))
    assert not store.record_matches(replace(record, metadata={'x': [2, 1]}))
    path = store._resolve_content_path(stored.content_path)
    path.write_text('corrupted')
    assert not store.record_matches(record), '数据库hash未变也要发现坏正文文件'
    store.upsert_record(record)
    path.unlink()
    assert not store.record_matches(record), '缺文件只有preview，不能误当整份正文一致'
    short = replace(record, content='short')
    store.upsert_record(short)
    path.unlink()
    assert store.record_matches(short), '短正文缺文件时沿原preview精确比较'


def test_reindex_changed_row_repairs_content_and_keeps_verification(tmp_path):
    agent, thread, path = synthetic_case(tmp_path, 3)
    lines = path.read_text().splitlines()
    row = json.loads(lines[0])
    row['content'] = 'new visible content'
    row['metadata']['operation_verification'] = {'schema': 'operation_verification.public.v1', 'status': 'succeeded'}
    lines[0] = json.dumps(row)
    path.write_text('\n'.join(lines) + '\n')
    ensure_thread_history_indexed(agent, agent.conversation_store, thread.thread_id)
    stored = agent.local_store.get_record(agent.local_store.make_record_id('conversation_message', f'{thread.thread_id}:m-0'))
    assert stored.content == row['content']
    assert stored.metadata['operation_verification'] == row['metadata']['operation_verification']
    assert thread.thread_id in agent._conversation_indexed_threads


def test_corrupt_transcript_leaves_index_and_batch_unfinished(tmp_path):
    agent, thread, path = synthetic_case(tmp_path, 3)
    before = agent.local_store.get_record(agent.local_store.make_record_id('conversation_message', f'{thread.thread_id}:m-0'))
    with path.open('ab') as handle:
        handle.write(b'not-json\n')
    with pytest.raises(OSError):
        ensure_thread_history_indexed(agent, agent.conversation_store, thread.thread_id)
    assert thread.thread_id not in agent._conversation_indexed_threads
    assert not hasattr(agent.local_store._connection_local, 'connection')
    after = agent.local_store.get_record(before.id)
    assert before == after


def _disk_rows(tmp_path, rows):
    path = tmp_path / 'synthetic.jsonl'
    addresses, offset = [], 0
    with path.open('wb') as handle:
        for row in rows:
            raw = (json.dumps(asdict(row)) + '\n').encode()
            addresses.append(MessageRowAddress(offset, len(raw), hashlib.sha256(raw).digest()))
            handle.write(raw)
            offset += len(raw)
    stat = path.stat()
    return MessageSnapshotRows(path, 'thread', offset, (stat.st_dev, stat.st_ino), tuple(addresses))


@pytest.mark.parametrize('disk', [False, True])
@pytest.mark.parametrize('identity', ['', 'same-turn'])
def test_prepared_native_replay_matches_anonymous_and_identified_last_envelopes(tmp_path, identity, disk):
    envelope = {'canonical_native_messages': {'schema': 'conversation_native_messages.v1', 'messages': [
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'envelope'}]}]}}
    if identity:
        envelope['conversation_request_id'] = identity
    plain = {'conversation_request_id': identity} if identity else {}
    rows = (
        MessageLogEntry('duplicate', 'thread', 'user', 'visible', metadata=plain),
        MessageLogEntry('duplicate', 'thread', 'assistant', 'final', metadata=envelope),
        MessageLogEntry('', 'thread', 'assistant', 'anonymous', metadata=envelope),
        MessageLogEntry('other', 'thread', 'user', 'legacy'),
    )
    expected = provider_history_messages_from_rows(rows)
    if disk:
        rows = _disk_rows(tmp_path, rows)
    replay = prepare_native_history_replay(rows)
    first = list(replay)
    assert tuple(first) == expected
    first[0]['content'][0]['text'] = 'mutated caller copy'
    assert tuple(replay) == expected


def _unfinished_operation(store, fail):
    with store._connection() as conn:
        conn.execute("INSERT INTO batch_probe VALUES ('unfinished')")
        if fail:
            raise RuntimeError('partial write')


@pytest.mark.parametrize('fail', [False, True])
def test_batch_operation_exit_discards_uncommitted_sql(tmp_path, fail):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    with store._connection() as conn:
        conn.execute('CREATE TABLE batch_probe (value TEXT)')
        conn.commit()
    with store.connection_batch():
        try:
            _unfinished_operation(store, fail)
        except RuntimeError:
            pass
        with store._connection() as conn:
            conn.execute("INSERT INTO batch_probe VALUES ('committed')")
            conn.commit()
    with store._connection() as conn:
        assert [row[0] for row in conn.execute('SELECT value FROM batch_probe')] == ['committed']


def test_prepared_plain_rows_follow_original_mutable_source_semantics():
    metadata = {'canonical_native_messages': {'schema': 'conversation_native_messages.v1', 'messages': [
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'native'}]}]}}
    rows = (MessageLogEntry('id', 'thread', 'assistant', 'legacy', metadata=metadata),)
    replay = prepare_native_history_replay(rows)
    assert tuple(replay) == provider_history_messages_from_rows(rows)
    metadata.clear()
    expected = provider_history_messages_from_rows(rows)
    assert expected
    assert tuple(replay) == expected
