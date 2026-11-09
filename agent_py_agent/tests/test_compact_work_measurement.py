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


def synthetic_case(root, count, *, tool_result_bytes: int = 1024):
    """批量构造真实 JSONL 和搜索索引；默认小结果供其它合同测试继续复用。"""
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
            row, record = synthetic_row(thread.thread_id, index, body, path,
                tool_result_bytes=tool_result_bytes)
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


def synthetic_row(thread_id, index, body, path, *, tool_result_bytes: int = 1024):
    message_id, call_id = f'm-{index}', f'call-{index}'
    native = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': call_id, 'name': 'read_file', 'input': {}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'content': 'y' * tool_result_bytes}]},
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
        counts['connect_paths'][str(args[0]).split('?')[0]] += 1
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
    counts = Counter(connect_sites=Counter(), connect_paths=Counter(), get_record_calls=0, retrieved_body_bytes=0, comparison_body_bytes=0)
    install_counters(monkeypatch, agent.local_store, counts)
    install_scan_counters(monkeypatch, counts, rows)
    def summarize(_request):
        from agent_py_agent.agent.io.sqlite_scope import _SQLITE_ACTIVE_SCOPE

        scope = _SQLITE_ACTIVE_SCOPE.get()
        assert scope is not None
        assert all(not entry.in_use and not entry.conn.in_transaction for entry in scope.entries.values())
        return ModelResponse(text='保留合成测试工作及其原始来源。', backend='fake')

    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response', summarize)
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


# LLM: 两个规模共用原上限；以较少消息承载较大的真实原生工具结果，仍走索引、预检、摘要和检查点提交。
#   避免把数万来源逐键写不可变索引的文件系统开销混入CI工作量探针；不跳过阶段、不放宽内存或线性工作量约束。
# 函数用途: 在给定行数的合成线程上测一次完整压缩的工作量，并断言各项上限。
def _assert_compaction_work_bounded(tmp_path, monkeypatch, count: int, min_source_bytes: int,
                                  *, tool_result_bytes: int = 1024) -> None:
    agent, thread, path = synthetic_case(tmp_path, count, tool_result_bytes=tool_result_bytes)
    # 提交后的冷转（f74d1161e）会把已覆盖历史移出热文件，来源大小必须在压缩前量取。
    source_bytes = path.stat().st_size
    counts = measure_compaction(agent, thread, monkeypatch, rows=count)
    counts['rows'] = count
    counts['source_bytes'] = source_bytes
    counts['full_parse_equivalents'] = counts['canonical_json_calls'] / count
    print('COMPACTMEM_MEASUREMENT ' + json.dumps(counts, sort_keys=True))
    assert counts['source_bytes'] > min_source_bytes
    violations = {
        'full_parse_passes': counts['full_parse_passes'] > 44,
        'canonical_json_calls': counts['canonical_json_calls'] > count * 50,
        # local.db 原批次预算不变；新增两个持久节点库各独立 ro/rw 代次租约。
        'sqlite_connects': counts['connect_paths'][str(tmp_path / 'local.db')] > 1,
        'compact_sqlite_connects': any(
            (not path.endswith(('/indexes/.nodes.sqlite3', '/timeline/.nodes.sqlite3', '.jsonl.offsets.sqlite3'))
             or calls > (1 if path.endswith('.jsonl.offsets.sqlite3') else 2))
            for path, calls in counts['connect_paths'].items() if path != str(tmp_path / 'local.db')
        ),
        'compact_database_count': len(counts['connect_paths']) > 4,
        'get_record_calls': counts['get_record_calls'] > 0,
        # 峰值不随来源线性增长：来源的一半再加 4 MB 固定开销余量（小规模时解释器/索引的固定开销占大头）。
        'tracemalloc_peak': counts['tracemalloc_peak'] >= counts['source_bytes'] // 2 + 4_000_000,
    }
    assert not any(violations.values()), violations


# 函数用途: 64 条来源、每条 640 KB 原生工具结果；仍超过原 40 MB 下限且足以暴露逐行重扫。
@pytest.mark.slow
def test_large_synthetic_transcript_work_is_bounded(tmp_path, monkeypatch):
    _assert_compaction_work_bounded(tmp_path, monkeypatch, 64, 40_000_000, tool_result_bytes=640_000)


# 函数用途: 半数来源、相同单条结果大小；原 4 MB 下限及所有按行数计算的上限保持不变。
def test_synthetic_transcript_work_is_bounded(tmp_path, monkeypatch):
    _assert_compaction_work_bounded(tmp_path, monkeypatch, 32, 4_000_000, tool_result_bytes=640_000)


@pytest.mark.parametrize('tool_result_bytes', [None, 4096])
def test_synthetic_case_preserves_index_and_native_tool_pairs(tmp_path, tool_result_bytes):
    options = {} if tool_result_bytes is None else {'tool_result_bytes': tool_result_bytes}
    agent, thread, path = synthetic_case(tmp_path, 2, **options)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    for index, row in enumerate(rows):
        native = row['metadata']['canonical_native_messages']['messages']
        assert native[0]['content'][0] == {'type': 'tool_use', 'id': f'call-{index}', 'name': 'read_file', 'input': {}}
        assert native[1]['content'][0] == {'type': 'tool_result', 'tool_use_id': f'call-{index}',
            'content': 'y' * (1024 if tool_result_bytes is None else tool_result_bytes)}
        record_id = LocalStore.make_record_id('conversation_message', f'{thread.thread_id}:m-{index}')
        indexed = agent.local_store.get_record(record_id)
        assert indexed.content == row['content']
        assert indexed.metadata['authoritative_transcript'] == str(path)


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
