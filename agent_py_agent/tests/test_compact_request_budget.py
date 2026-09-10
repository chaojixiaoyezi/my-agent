import json
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
        assert candidate.tools == request.tools
        assert candidate.system_instruction == request.system_instruction
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


def test_segment_failure_and_stop_never_skip_to_later_sources(monkeypatch):
    request = _request("历史记录" * 10000)
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda r: calls.append(r) or ModelResponse(text="", backend="fake"))
    with pytest.raises(ConversationCompactError, match="有效摘要"):
        budget_module.generate_bounded_compact_response(request)
    assert len(calls) == 1
    with pytest.raises(InterruptedError):
        budget_module.generate_bounded_compact_response(request, interrupt_check=lambda: True)
    assert len(calls) == 1


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
