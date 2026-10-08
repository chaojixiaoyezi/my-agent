"""只测隔离合成历史：索引、正式 transcript 预检、摘要及检查点提交。"""
from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
import sys
import tracemalloc
from collections import Counter
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore, compact_request_budget
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    load_conversation_compact_source,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.history_index import ensure_thread_history_indexed
from agent_py_agent.agent.local_storage import LocalStore


def synthetic_case(root, count):
    store = ConversationStore(root / 'conversation')
    thread = store.threads.get_or_create({'canonical_user_id': 'synthetic-owner', 'now': 1})
    path = store.storage.message_path(thread.thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    local = LocalStore(root / 'local.db', enable_fts=False)
    body = 'synthetic visible output ' + 'x' * 1024
    blob = local.files_dir / 'synthetic-shared.txt'
    blob.write_text(body)
    records = []
    with path.open('wb') as handle:
        for index in range(count):
            row, record = synthetic_row(thread.thread_id, index, body, path)
            handle.write((json.dumps(row) + '\n').encode())
            records.append(record)
    with local._connection() as conn:
        conn.executemany('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', records)
        conn.commit()
    agent = SimpleNamespace(conversation_store=store, local_store=local,
        home_paths=SimpleNamespace(owner_compact_dir=root / 'compact'),
        backend=SimpleNamespace(name='fake', model_name='fake', max_tokens=128),
        prompts=SimpleNamespace(build=lambda prompt, *_a, **_k: prompt),
        config=SimpleNamespace(model_context_window_tokens=200_000, memory_compact_remote_enabled=False))
    return agent, thread, path


def synthetic_row(thread_id, index, body, path):
    message_id, call_id = f'm-{index}', f'call-{index}'
    native = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': call_id, 'name': 'read_file', 'input': {}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'content': 'y' * 1024}]},
    ]
    row = {'message_id': message_id, 'thread_id': thread_id, 'role': 'assistant', 'content': body,
           'metadata': {'conversation_request_id': f'r-{index}', 'canonical_native_messages': {
               'schema': 'conversation_native_messages.v1', 'messages': native}}}
    source_id = f'{thread_id}:{message_id}'
    metadata = {'thread_id': thread_id, 'message_id': message_id, 'role': 'assistant', 'channel': 'internal',
                'channel_message_id': '', 'created_at': 0.0, 'authoritative_transcript': str(path)}
    record = (LocalStore.make_record_id('conversation_message', source_id), 'conversation_message', source_id,
              'Conversation assistant', 'files/synthetic-shared.txt', hashlib.sha256(body.encode()).hexdigest(),
              body[:100], json.dumps(metadata, sort_keys=True), 'private', 0.0, 0.0)
    return row, record


def install_counters(monkeypatch, local, counts):
    loads, connect, get, read = json.loads, sqlite3.connect, local.get_record, local._read_content

    def measured_loads(value, *args, **kwargs):
        counts['json_calls'] += 1
        if isinstance(value, (str, bytes)) and value.startswith(b'{"message_id":' if isinstance(value, bytes) else '{"message_id":'):
            counts['canonical_json_calls'] += 1
        return loads(value, *args, **kwargs)

    def measured_connect(*args, **kwargs):
        counts['sqlite_connects'] += 1
        frame = sys._getframe(1)
        names = []
        for _ in range(5):
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        counts['connect_sites']['/'.join(names)] += 1
        return connect(*args, **kwargs)

    def measured_get(record_id):
        counts['get_record_calls'] += 1
        return get(record_id)

    def measured_read(row):
        text = read(row)
        counts['retrieved_body_bytes'] += len(text.encode())
        return text

    monkeypatch.setattr(json, 'loads', measured_loads)
    monkeypatch.setattr(sqlite3, 'connect', measured_connect)
    monkeypatch.setattr(local, 'get_record', measured_get)
    monkeypatch.setattr(local, '_read_content', measured_read)
    compare = getattr(local, '_content_matches', None)
    if compare is not None:
        def measured_compare(row, content):
            matched = compare(row, content)
            if matched:
                counts['comparison_body_bytes'] += len(content.encode())
            return matched
        monkeypatch.setattr(local, '_content_matches', measured_compare)


def measure_compaction(agent, thread, monkeypatch, rows: int = 20_000):
    counts = Counter(connect_sites=Counter(), get_record_calls=0, retrieved_body_bytes=0, comparison_body_bytes=0)
    install_counters(monkeypatch, agent.local_store, counts)
    install_scan_counters(monkeypatch, counts, rows)
    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response',
                        lambda _r: ModelResponse(text='保留合成测试工作及其原始来源。', backend='fake'))
    gc.collect()
    tracemalloc.start()
    try:
        ensure_thread_history_indexed(agent, agent.conversation_store, thread.thread_id)
        source = load_conversation_compact_source(agent, agent.conversation_store, thread, scope=THREAD_COMPACT_SCOPE)
        result = prepare_conversation_context(agent, agent.conversation_store, thread, options=ConversationCompactOptions(
            source=source, force=True, exclude_request_id='current-request',
            provider_surface=ConversationCompactProviderSurface('稳定前缀', None, '摘要系统')))
        counts['tracemalloc_peak'] = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result.compacted and result.thread.compact_generation == 1
    return counts


def install_scan_counters(monkeypatch, counts, count):
    from agent_py_agent.agent.conversation import message_selection, store_messages
    from agent_py_agent.agent.conversation.message_replay import MessageSnapshotRows

    validate, entries = store_messages._validate_all, store_messages._validated_entries
    scan, replay = message_selection.scan_message_snapshot, MessageSnapshotRows.__iter__

    def measured_validate(path):
        result = validate(path)
        if not result[0] and result[1] == count:
            counts['full_parse_passes'] += 1
        return result

    def counted_iterator(iterator, expected):
        visited = 0
        try:
            for row in iterator:
                visited += 1
                yield row
        finally:
            iterator.close()
            if expected == count and visited == count:
                counts['full_parse_passes'] += 1

    def measured_entries(*args):
        return counted_iterator(entries(*args), args[2])

    def measured_scan(*args, **kwargs):
        result = scan(*args, **kwargs)
        counts['full_parse_passes'] += 1
        return result

    def measured_replay(rows):
        return counted_iterator(replay(rows), len(rows))

    monkeypatch.setattr(store_messages, '_validate_all', measured_validate)
    monkeypatch.setattr(store_messages, '_validated_entries', measured_entries)
    monkeypatch.setattr(message_selection, 'scan_message_snapshot', measured_scan)
    monkeypatch.setattr(MessageSnapshotRows, '__iter__', measured_replay)


# LLM: 两个规模共用同一组上限（遍数、每行解析次数、连接数、整行读取、峰值相对来源大小）；2 万行版本在 CI 慢机上会超过
#   单测 60 秒上限（10-08 25d4538db 的 3.11 job 超时），所以标 slow 只在本机/压测跑，CI 跑 2,000 行版本守同一合同。
# 函数用途: 在给定行数的合成线程上测一次完整压缩的工作量，并断言各项上限。
def _assert_compaction_work_bounded(tmp_path, monkeypatch, count: int, min_source_bytes: int) -> None:
    agent, thread, path = synthetic_case(tmp_path, count)
    counts = measure_compaction(agent, thread, monkeypatch, rows=count)
    counts['rows'] = count
    counts['source_bytes'] = path.stat().st_size
    counts['full_parse_equivalents'] = counts['canonical_json_calls'] / count
    print('COMPACTMEM_MEASUREMENT ' + json.dumps(counts, sort_keys=True))
    assert counts['source_bytes'] > min_source_bytes
    violations = {
        'full_parse_passes': counts['full_parse_passes'] > 44,
        'canonical_json_calls': counts['canonical_json_calls'] > count * 50,
        'sqlite_connects': counts['sqlite_connects'] > 1,
        'get_record_calls': counts['get_record_calls'] > 0,
        # 峰值不随来源线性增长：来源的一半再加 4 MB 固定开销余量（小规模时解释器/索引的固定开销占大头）。
        'tracemalloc_peak': counts['tracemalloc_peak'] >= counts['source_bytes'] // 2 + 4_000_000,
    }
    assert not any(violations.values()), violations


# 函数用途: 2 万行、约 50 MB 的完整规模测量（slow：本机复核和压测用）。
@pytest.mark.slow
def test_large_synthetic_transcript_work_is_bounded(tmp_path, monkeypatch):
    _assert_compaction_work_bounded(tmp_path, monkeypatch, 20_000, 40_000_000)


# 函数用途: 2,000 行、约 5 MB 的同一合同，CI 每次都跑。
def test_synthetic_transcript_work_is_bounded(tmp_path, monkeypatch):
    _assert_compaction_work_bounded(tmp_path, monkeypatch, 2_000, 4_000_000)


def test_remote_rejection_does_not_materialize_unbudgeted_history(tmp_path):
    from agent_py_agent.agent.conversation.compact import _remote_transcript_summary
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource

    agent, _, _ = synthetic_case(tmp_path, 2)
    agent.config.memory_compact_remote_enabled = True
    agent.config.model_context_window_tokens = 1000
    agent.backend.supports_remote_compaction = lambda: True
    call = SimpleNamespace(provider_surface=ConversationCompactProviderSurface('前缀', None, '系统'),
                           request_id='', run_id='', task_id='', thread_id='', calibration=None)

    def history():
        for index in range(64):
            yield {'role': 'user', 'content': [{'type': 'text', 'text': str(index) + 'z' * 524288}]}

    gc.collect()
    tracemalloc.start()
    try:
        assert _remote_transcript_summary(agent, call, CompactMessageSource(history)) == ''
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    print(f'REMOTE_REJECTION_PEAK {peak}')
    assert peak < 4_000_000, '预算拒绝前物化了完整来源'
