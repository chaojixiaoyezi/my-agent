"""原生历史投影只复制隔离边界，保留嵌套写隔离与完整工具往返。"""
from __future__ import annotations

import gc
import tracemalloc
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.message_adapter import strip_orphaned_tool_blocks
from agent_py_agent.agent.conversation.compact_provider_surface import (
    conversation_compact_provider_messages,
)
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
    canonical_native_messages_from_metadata,
    provider_history_messages_from_rows,
)


# LLM: 只构造内存canonical元数据，所有大内容在峰值测量前生成；真实normalizer/投影/孤儿修补不替身。
# 函数用途: 以嵌套工具结果验证容器深拷贝开销，不用字符串副本数量冒充真实内存改进。
def _rows(*, anonymous=False, blocks=2):
    native = [
        {'role': 'user', 'content': [{'type': 'text', 'text': '核对原始材料'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'read-1', 'name': 'read_file', 'input': {'paths': ['a']}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'read-1', 'content': [
            {'type': 'text', 'text': f'完整字段-{index}', 'detail': {'items': [index, '保留嵌套事实']}}
            for index in range(blocks)
        ]}]},
    ]
    meta = {CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(native)}
    if not anonymous:
        meta['conversation_request_id'] = 'old-request'
    return [SimpleNamespace(message_id='final-1', role='assistant', content='已经核对', metadata=meta)], native


# LLM: tracemalloc只包围投影，不改变产品GC行为或调用内部copy计数，返回值在测量结束前保持存活。
# 函数用途: 比较相同输入一次隔离复制与完整Compact投影的实际Python峰值分配。
def _peak(callback):
    gc.collect()
    tracemalloc.start()
    try:
        result = callback()
        _, peak = tracemalloc.get_traced_memory()
        return result, peak
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize('anonymous', [False, True])
def test_compact_projection_does_not_duplicate_detached_native_containers(anonymous):
    rows, native = _rows(anonymous=anonymous, blocks=4000)
    direct, baseline = _peak(lambda: canonical_native_messages_from_metadata(rows[0].metadata))
    projected, peak = _peak(lambda: conversation_compact_provider_messages('', 0, rows))
    assert projected == list(direct) == native
    print(f'native projection anonymous={anonymous} baseline={baseline} peak={peak}')
    assert peak < baseline * 1.25, '隔离后的完整嵌套历史被再次深拷贝'


@pytest.mark.parametrize('anonymous', [False, True])
def test_projections_keep_canonical_and_other_call_detached(anonymous):
    rows, native = _rows(anonymous=anonymous)
    first = conversation_compact_provider_messages('', 0, rows)
    other = provider_history_messages_from_rows(rows)
    first[1]['content'][0]['input']['paths'].append('changed')
    first[2]['content'][0]['content'][0]['detail']['items'].append('changed')
    assert list(other) == native
    assert list(canonical_native_messages_from_metadata(rows[0].metadata)) == native
    rows[0].metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY]['messages'][2]['content'][0]['content'][1]['detail']['items'].append('canonical-change')
    assert list(other) == native


def test_repeated_anonymous_rows_preserve_independent_outputs():
    rows, native = _rows(anonymous=True)
    projected = provider_history_messages_from_rows([rows[0], rows[0]])
    assert list(projected) == native + native
    projected[1]['content'][0]['input']['paths'].append('changed')
    assert projected[len(native) + 1] == native[1]
    assert list(canonical_native_messages_from_metadata(rows[0].metadata)) == native


def test_identified_final_replaces_prose_once_and_orphan_repair_stays_exact():
    rows, native = _rows()
    rows.insert(0, SimpleNamespace(message_id='user-1', role='user', content='展示原始要求',
                                   metadata={'conversation_request_id': 'old-request'}))
    rows.append(SimpleNamespace(message_id='other', role='assistant', content='独立回复', metadata={}))
    assert list(provider_history_messages_from_rows(rows)) == native + [
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '独立回复'}]},
    ]
    missing = deepcopy(native[:2])
    missing.append({'role': 'user', 'content': [
        {'type': 'text', 'text': '文字保留'}, {'type': 'tool_result', 'tool_use_id': 'unknown', 'content': '孤儿'},
    ]})
    rows[1].metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = canonical_native_messages_envelope(missing)
    raw = provider_history_messages_from_rows(rows)
    expected = strip_orphaned_tool_blocks(deepcopy(list(raw)))
    assert conversation_compact_provider_messages('', 0, rows) == expected
    assert rows[1].metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY]['messages'] == missing
