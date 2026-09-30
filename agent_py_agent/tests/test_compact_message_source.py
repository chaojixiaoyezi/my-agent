"""可重放provider数组编码、唯一估算和运输边界等价性。"""
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import compact_request_budget as budget
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_message_source import (
    REASONING_CIPHERTEXT_PLACEHOLDER,
    CompactMessageSource,
    estimate_compact_messages,
    estimate_compact_payload,
    summary_source_message,
)
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests.test_compact_request_budget import _request, segment_part


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
        # 有工具面时摘要请求只多一个 none 选择（保留工具定义以复用缓存前缀），其余与原请求逐项相同。
        assert isinstance(actual.messages, list) and actual.tool_choice.mode == 'none'
        assert actual == replace(original, tool_choice=actual.tool_choice)
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


# 函数用途: 一段带 Responses 加密思考的历史：用户长要求、助手轮里一个加密思考块（带可读摘要）和正文。
def _reasoning_history(cipher: str = "Q0lQSEVS" * 4000, user_suffix: str = "") -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "用户要求必须保留🪴\n" * 600 + user_suffix}]},
        {"role": "assistant", "content": [
            {"type": "responses_reasoning", "model": "gpt-test", "item": {
                "type": "reasoning", "id": "rs_1", "encrypted_content": cipher,
                "summary": [{"type": "summary_text", "text": "可读思考摘要"}]}},
            {"type": "text", "text": "助手回复" * 300},
        ]},
    ]


def _segment_collector(monkeypatch):
    segments = []

    def generate(candidate):
        assert candidate.messages is None
        segments.append(segment_part(candidate.prompt)[3])
        return ModelResponse(text="保留先前事实与本段的新事实", backend="fake")

    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    return segments


@pytest.mark.parametrize("replayed", [False, True])
def test_segment_source_omits_reasoning_ciphertext_but_keeps_its_summary(monkeypatch, replayed):
    # 09-30 生产实测：每个 GPT 助手轮一段约 3.6K 字符的 base64 密文，摘要模型读不懂，只白占分段来源。
    messages = _reasoning_history()
    before = deepcopy(messages)
    original = replace(_request(""), messages=messages)
    source = CompactMessageSource(lambda: iter(messages)) if replayed else None
    segments = _segment_collector(monkeypatch)
    budget.generate_bounded_compact_response(replace(original, messages=None) if replayed else original, message_source=source)
    joined = "".join(segments)
    assert len(segments) > 1
    assert joined == json.dumps([summary_source_message(message) for message in messages], ensure_ascii=False)
    assert "Q0lQSEVS" not in joined and REASONING_CIPHERTEXT_PLACEHOLDER in joined
    assert "可读思考摘要" in joined and '"id": "rs_1"' in joined and "用户要求必须保留" in joined
    assert messages == before


def test_fitting_summary_request_still_sends_reasoning_ciphertext_natively(monkeypatch):
    messages = _reasoning_history(cipher="c" * 50)
    messages[0]["content"][0]["text"] = "短要求"
    messages[1]["content"][1]["text"] = "短回复"
    seen = []

    def generate(actual):
        seen.append(actual.messages)
        return ModelResponse(text="摘要", backend="fake")

    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    budget.generate_bounded_compact_response(replace(_request(""), messages=messages))
    assert seen == [messages]


def test_projected_source_closes_original_on_early_json_exit():
    source, opened = _tracked_source(_reasoning_history())
    parts = source.projected(summary_source_message).json_parts()
    assert next(parts) == "["
    next(parts)
    assert any(iterator.gi_frame is not None for iterator in opened)
    parts.close()
    assert all(iterator.gi_frame is None for iterator in opened)


def test_projection_keeps_two_pass_change_detection_for_readable_content(monkeypatch):
    calls = {"n": 0}

    # 函数用途: 每次重放都换一份密文（投影后相同，允许）；可选地每次也改可读正文（必须判为来源改变）。
    def factory(change_text):
        def replay():
            calls["n"] += 1
            return iter(_reasoning_history(cipher=f"C{calls['n']}" * 4000,
                                           user_suffix=f"第{calls['n']}次" if change_text else ""))
        return replay

    _segment_collector(monkeypatch)
    request = replace(_request(""), messages=None)
    assert budget.generate_bounded_compact_response(request, message_source=CompactMessageSource(factory(False))).text
    with pytest.raises(ConversationCompactError) as error:
        budget.generate_bounded_compact_response(request, message_source=CompactMessageSource(factory(True)))
    assert error.value.code == "COMPACT_SOURCE_CHANGED"
