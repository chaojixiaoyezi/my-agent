"""可重放provider数组编码、唯一估算和运输边界等价性。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import compact_request_budget as budget
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_message_source import (
    CompactMessageSource,
    estimate_compact_messages,
    estimate_compact_payload,
)
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests.test_compact_request_budget import _request


@pytest.mark.parametrize('messages', [[], [{'role': 'user', 'content': '中文🪴\n\\"'}],
    [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call', 'input': {'z': False, 'a': None}}]},
     {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': ['原文', {'数量': 3}]}]}],
    [{'role': 'user', 'content': '大单行' * 200000}]])
def test_replayed_json_and_tokens_equal_original_values(messages):
    source = CompactMessageSource(lambda: iter(messages))
    assert ''.join(source.json_parts()) == json.dumps(messages, ensure_ascii=False)
    assert estimate_compact_messages(source) == estimate_tokens(messages)
    payload = {'prompt': '稳定要求', 'messages': messages, 'tools': [], 'system_instruction': '系统'}
    assert estimate_compact_payload({**payload, 'messages': source}) == estimate_tokens(payload)


def test_fitting_source_materializes_only_at_original_transport(monkeypatch):
    original = _request('短原文')
    source = CompactMessageSource(lambda: iter(original.messages))
    request = replace(original, messages=None)
    seen = []

    def generate(actual):
        assert isinstance(actual.messages, list) and actual == original
        seen.append(True)
        return ModelResponse(text='摘要', backend='fake')

    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', generate)
    assert budget.generate_bounded_compact_response(request, message_source=source).text == '摘要'
    assert seen == [True] and request.messages is None
    with pytest.raises(ValueError, match='one message source'):
        budget.generate_bounded_compact_response(original, message_source=source)


# LLM: 保留每次实际生成器的强引用，保证断言检查显式close，而不是CPython引用计数偶然回收。
# 函数用途: 用可观察迭代器锁定组合来源和非文本短路的关闭合同。
def _tracked_source(messages):
    opened = []

    class TrackedSource(CompactMessageSource):
        def __iter__(self):
            iterator = super().__iter__()
            opened.append(iterator)
            return iterator

    return TrackedSource(lambda: iter(messages)), opened


def test_with_tail_closes_original_source_on_early_json_exit():
    source, opened = _tracked_source([{'role': 'user', 'content': '第一行'}, {'role': 'assistant', 'content': '第二行'}])
    parts = source.with_tail([{'role': 'user', 'content': '工具尾部'}]).json_parts()
    assert next(parts) == '['
    next(parts)
    assert any(iterator.gi_frame is not None for iterator in opened)
    parts.close()
    assert all(iterator.gi_frame is None for iterator in opened)


def test_nontext_short_circuit_closes_replayed_source():
    source, opened = _tracked_source([
        {'role': 'user', 'content': [{'type': 'unknown_modality', 'ref': 'canonical-ref'}]},
        {'role': 'assistant', 'content': '过长文本' * 5000},
    ])
    with pytest.raises(ConversationCompactError) as error:
        budget.generate_bounded_compact_response(replace(_request(''), messages=None), message_source=source)
    assert error.value.code == 'COMPACT_SOURCE_NON_TEXT'
    assert opened and all(iterator.gi_frame is None for iterator in opened)
