"""单次无正文摘要必须有界原样重试，不能一次失误就让整份摘要机械降级。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import compact_request_budget as budget
from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
from agent_py_agent.tests.test_compact_request_budget import _request


@pytest.mark.parametrize("preserve_prefix", [False, True])
def test_single_empty_summary_retries_exact_surface_before_whole_fallback(monkeypatch, preserve_prefix):
    request = _request("完整用户约束和已验证进度")
    original = request.messages
    source = CompactMessageSource(lambda: iter(original), preserve_prefix=preserve_prefix)
    calls = []

    def generate(candidate):
        calls.append(candidate)
        return ModelResponse(text="" if len(calls) == 1 else "完整摘要：保留用户约束与已验证进度", backend="fake")

    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    result = budget.generate_bounded_compact_response(replace(request, messages=None), message_source=source)
    assert result.text == "完整摘要：保留用户约束与已验证进度"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0].messages == original and calls[0].tools == request.tools
    assert "mechanical" not in result.text


def test_persistent_empty_summary_has_only_one_same_surface_retry(monkeypatch):
    request = _request("历史保持")
    calls = []
    empty = ModelResponse(text=" ", backend="fake")
    monkeypatch.setattr(budget, "generate_auxiliary_model_response", lambda candidate: calls.append(candidate) or empty)
    result = budget.generate_bounded_compact_response(request)
    assert len(calls) == 2 and result is empty
    assert calls[0] == calls[1]


def test_cancel_after_empty_reply_never_sends_repair(monkeypatch):
    calls = []
    monkeypatch.setattr(budget, "generate_auxiliary_model_response",
                        lambda candidate: calls.append(candidate) or ModelResponse(text="", backend="fake"))
    with pytest.raises(InterruptedError):
        budget.generate_bounded_compact_response(_request("历史"), interrupt_check=lambda: bool(calls))
    assert len(calls) == 1


def test_truncated_reply_has_no_empty_retry_or_duplicate_observation(monkeypatch, caplog):
    request = _request("原始来源不变")
    calls = []
    response = ModelResponse(text="", backend="fake", truncated=True, stop_reason="max_tokens")
    monkeypatch.setattr(budget, "generate_auxiliary_model_response", lambda candidate: calls.append(candidate) or response)
    actual = budget.generate_bounded_compact_response(request)
    assert actual is response and len(calls) == 1
    assert not [row for row in caplog.records if hasattr(row, "compact_response_shape")]
    assert budget.compact_summary_response_outcome(actual, reject_truncated=False, request=request) == ("", "EMPTY")
    diagnostics = [row.compact_response_shape for row in caplog.records if hasattr(row, "compact_response_shape")]
    assert len(diagnostics) == 1 and diagnostics[0]["truncated"] is True
