import json
import re
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.auxiliary_model_call import AuxiliaryModelCallRequest
from agent_py_agent.agent.conversation.compact_guard import (
    ConversationCompactError,
    split_recent_complete_turns,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt


# LLM: Fake requests have an explicit small model window and no live credential or persistence.
# 函数用途: 构造大窗口切小窗口的确定性边界，验证每个源字符均进入摘要而不发网络请求。
def _request(text: str) -> AuxiliaryModelCallRequest:
    agent = SimpleNamespace(
        backend=SimpleNamespace(max_tokens=128),
        config=SimpleNamespace(model_context_window_tokens=2000, model_context_window_explicit=True),
    )
    return AuxiliaryModelCallRequest(
        agent=agent, prompt="请总结历史", system_instruction="稳定系统前缀",
        tools=[{"name": "read_file"}],
        messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
        purpose="conversation_compact_summary",
    )


# LLM: Mechanical fallback markers are the only place a degraded segment records which source bytes
# it stands for, so contiguity here is the regression guard for "never drop history silently".
# 函数用途: 校验降级摘要的覆盖区间首尾相接且铺满整段源历史，任何源字节都没有被跳过。
def responses_cover_source(text: str, total: int) -> bool:
    ranges = [
        (int(start), int(end))
        for start, end in re.findall(r"- source_range: \[(\d+):(\d+)/\d+\]", text)
    ]
    if not ranges:
        return False
    if ranges[0][0] != 0 or ranges[-1][1] != total:
        return False
    return all(previous_end == start for (_, previous_end), (start, _) in zip(ranges, ranges[1:]))


def test_fitting_request_preserves_exact_cache_surface(monkeypatch):
    request = _request("记住蓝色项目，稍后继续")
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda r: calls.append(r) or "result")
    assert budget_module.generate_bounded_compact_response(request) == "result"
    assert calls == [request]
    assert calls[0] is request


def test_large_native_history_is_fully_covered_with_bounded_calls(monkeypatch):
    request = _request("中间也必须保留🪴\n" * 3000)
    segments = []
    progress = []

    def generate(candidate):
        assert candidate.prompt == request.prompt
        assert candidate.tools == []
        assert candidate.tool_choice.mode == "none"
        assert candidate.system_instruction != request.system_instruction
        assert budget_module._request_tokens(candidate) <= 1472
        text = candidate.messages[0]["content"][0]["text"]
        segments.append(text.split("]：\n", 1)[1])
        return ModelResponse(text="保留先前事实与本段的新事实", backend="fake")

    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", generate)
    budget_module.generate_bounded_compact_response(request, source_progress=lambda covered, total: progress.append((covered, total)))
    assert len(segments) > 2
    assert "".join(segments) == json.dumps(request.messages, ensure_ascii=False)
    assert [covered for covered, _ in progress] == sorted({covered for covered, _ in progress})
    assert len(progress) == len(segments)
    assert progress[-1][0] == progress[-1][1] == len(json.dumps(request.messages, ensure_ascii=False))


def test_typed_provider_overflow_reduces_request_without_losing_source(monkeypatch):
    request = _request("abcdefghij" * 60)
    calls = []

    def generate(candidate):
        calls.append(candidate)
        if len(calls) == 1:
            raise ProviderContextWindowError("provider has a smaller window")
        return ModelResponse(text="摘要", backend="fake")

    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", generate)
    budget_module.generate_bounded_compact_response(request)
    assert calls[0] is request
    assert len(calls) > 1
    assert "".join(r.messages[0]["content"][0]["text"].split("]：\n", 1)[1] for r in calls[1:]) == json.dumps(request.messages, ensure_ascii=False)


def test_degraded_segment_keeps_source_coverage_and_stops_on_interrupt(monkeypatch, caplog):
    request = _request("历史记录" * 10000)
    calls = []
    progress = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda r: calls.append(r) or ModelResponse(text="", backend="fake"))
    response = budget_module.generate_bounded_compact_response(request, source_progress=lambda *p: progress.append(p))
    total = len(json.dumps(request.messages, ensure_ascii=False))
    assert progress[-1] == (total, total)
    # 每个片段最多一次首答加有界纠正，仍无有效摘要就机械降级，不再让整轮压缩作废。
    assert len(calls) % (1 + budget_module._SEGMENT_REPAIR_LIMIT) == 0
    assert responses_cover_source(response.text, total)
    assert response.tool_use_blocks == [] and response.truncated is False
    assert "PRIVATE-HISTORY" not in caplog.text
    before = len(calls)
    with pytest.raises(InterruptedError):
        budget_module.generate_bounded_compact_response(request, interrupt_check=lambda: True)
    assert len(calls) == before


def test_persistent_truncation_shrinks_once_then_degrades_without_fragmenting(monkeypatch):
    request = _request("历史记录" * 10000)
    healthy = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda r: healthy.append(r) or ModelResponse(text="摘要", backend="fake"))
    budget_module.generate_bounded_compact_response(request)
    truncated = []
    monkeypatch.setattr(
        budget_module,
        "generate_auxiliary_model_response",
        lambda r: truncated.append(r) or ModelResponse(text="只有前半段", backend="fake", truncated=True),
    )
    response = budget_module.generate_bounded_compact_response(request)
    total = len(json.dumps(request.messages, ensure_ascii=False))
    # 缩段只允许到原片段一半，避免把一个片段碎成大量微小片段而放大调用次数。
    assert len(truncated) <= 8 * len(healthy)
    assert "只有前半段" not in response.text
    assert responses_cover_source(response.text, total)


def test_segment_surface_retains_summary_rules_not_executor_prefix(monkeypatch):
    from dataclasses import replace

    request = replace(_request("旧任务继续执行" * 3000), prompt=CacheStructuredPrompt(
        "调用工具直到工作完成", "保留任务 A 的端口 3000；用户要求重点保留验收结果"
    ))
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response",
                        lambda r: calls.append(r) or ModelResponse(text="摘要", backend="fake"))
    budget_module.generate_bounded_compact_response(request)
    assert len(calls) > 1
    assert all(r.prompt == request.prompt.cache_layout.volatile_suffix for r in calls)
    assert all(r.tools == [] and r.tool_choice.mode == "none" for r in calls)


@pytest.mark.parametrize(("response", "code", "hint"), [
    (ModelResponse(text="", backend="fake"), "EMPTY", "没有输出摘要正文"),
    (
        ModelResponse(text="执行工作", backend="fake", tool_use_blocks=[{"name": "run_command"}]),
        "TOOL_CALL",
        "不要输出工具调用",
    ),
])
def test_invalid_segment_repairs_in_place_then_degrades(monkeypatch, caplog, response, code, hint):
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda r: calls.append(r) or response)
    result = budget_module.generate_bounded_compact_response(_request("很短的历史记录" * 500))
    texts = [call.messages[0]["content"][0]["text"] for call in calls]
    first_segment = texts[: 1 + budget_module._SEGMENT_REPAIR_LIMIT]
    assert "纠正要求" not in first_segment[0]
    assert hint in first_segment[1] and hint in first_segment[2]
    # 同一条源片段被原地纠正，覆盖区间不变。
    assert len({text.split("历史 JSON 连续片段 ", 1)[1].split("：", 1)[0] for text in first_segment}) == 1
    assert f"- reason: {code}" in result.text
    assert "执行工作" not in result.text
    assert "很短的历史记录" not in caplog.text and "执行工作" not in caplog.text


def test_final_segment_failure_returns_carried_summary_not_raw_reply(monkeypatch):
    request = _request("历史记录" * 10000)
    calls = []

    def generate(candidate):
        calls.append(candidate)
        if len(calls) == 1:
            return ModelResponse(text="第一段已确认的摘要", backend="fake")
        return ModelResponse(text="只有后半段", backend="fake", truncated=True)

    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", generate)
    response = budget_module.generate_bounded_compact_response(request)
    # 末段原始答复不完整时，返回值必须是已校验的累计摘要，否则调用方会拿残缺正文当整段历史。
    assert response.truncated is False and response.tool_use_blocks == []
    assert response.stop_reason == ""
    assert "第一段已确认的摘要" in response.text
    assert "[compact-segment-mechanical-fallback]" in response.text
    assert "只有后半段" not in response.text


def test_auxiliary_success_metrics_are_recorded(monkeypatch):
    from agent_py_agent.agent.conversation import auxiliary_model_call as auxiliary

    response = ModelResponse(text="摘要", backend="fake")
    recorded = []
    agent = SimpleNamespace(backend=SimpleNamespace(generate=lambda *a, **k: response))
    monkeypatch.setattr(auxiliary, "_start_auxiliary_call", lambda *a: (object(), "call"))
    monkeypatch.setattr(auxiliary, "_invoke_auxiliary_generate", lambda *a: response)
    monkeypatch.setattr(auxiliary, "record_model_call_finished", lambda *a: None)
    monkeypatch.setattr(auxiliary, "_record_auxiliary_cost", lambda *a: None)
    monkeypatch.setattr(auxiliary, "record_llm_call", lambda *a, **k: recorded.append(k))
    assert auxiliary.generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent, "摘要")) is response
    assert recorded == [{"ok": True}]


def test_tail_budget_counts_hidden_native_tool_history():
    rows = [
        MessageLogEntry(message_id="u1", thread_id="t", role="user", content="第一项"),
        MessageLogEntry(message_id="a1", thread_id="t", role="assistant", content="好了"),
        MessageLogEntry(message_id="u2", thread_id="t", role="user", content="继续"),
        MessageLogEntry(message_id="a2", thread_id="t", role="assistant", content="短答复", metadata={
            "canonical_native_messages": canonical_native_messages_envelope([
                {"role": "user", "content": [{"type": "text", "text": "工具输出" * 10000}]},
            ]),
        }),
    ]
    prefix, tail = split_recent_complete_turns(rows, max_turns=1, max_tail_tokens=1000)
    assert prefix == rows
    assert tail == []
