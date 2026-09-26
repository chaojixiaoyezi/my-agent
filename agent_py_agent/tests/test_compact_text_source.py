"""顺序摘要字符来源的完整性、失败和峰值内存边界。"""
from __future__ import annotations

import hashlib
import json
import tracemalloc
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import compact_request_budget as budget
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_text_source import CompactTextSource
from agent_py_agent.tests.test_compact_request_budget import _request, segment_part


def test_unicode_slices_retry_and_release_preserve_json_characters():
    messages = [{"content": '前🪴\\"\n' * 5000}, {"content": "尾部"}]
    expected = json.dumps(messages, ensure_ascii=False)
    def factory():
        return json.JSONEncoder(ensure_ascii=False).iterencode(messages)
    with CompactTextSource(factory) as source:
        assert len(source) == len(expected)
        for start in range(0, len(source), 1237):
            end = min(len(source), start + 1237)
            assert source[start:end] == source[start:end] == expected[start:end]
            source.discard_before(end)
            if end:
                with pytest.raises(ValueError):
                    source[0:end]
        source.finish()


@pytest.mark.parametrize("mutation", ["replace", "append", "truncate"])
def test_second_scan_rejects_changed_source(mutation):
    calls = 0

    def replay():
        nonlocal calls
        calls += 1
        yield "abcd" if calls == 1 else {"replace": "ABCD", "append": "abcde", "truncate": "abc"}[mutation]

    with CompactTextSource(replay) as source, pytest.raises(ConversationCompactError) as error:
        source[0:len(source)]
        source.finish()
    assert error.value.code == "COMPACT_SOURCE_CHANGED"


@pytest.mark.parametrize("stop_during_measure", [False, True])
def test_cancel_closes_upstream_and_keeps_no_source_window(stop_during_measure):
    closed = []
    stop = False

    def replay():
        nonlocal stop
        try:
            yield "x" * 10000
            if stop_during_measure:
                stop = True
            yield "后续字符"
        finally:
            closed.append(True)

    with pytest.raises(InterruptedError):
        with CompactTextSource(replay, lambda: stop) as source:
            source[0:100]
            stop = True
            source.finish()
    assert len(closed) == (1 if stop_during_measure else 2)


@pytest.mark.parametrize("reason", ["EMPTY", "TOOL_CALL", "TRUNCATED"])
def test_every_corrective_request_fits_same_budget(monkeypatch, reason):
    invalid = {"EMPTY": ModelResponse(text="", backend="fake"),
               "TOOL_CALL": ModelResponse(text="", backend="fake", tool_use_blocks=[{"name": "do_not_run"}]),
               "TRUNCATED": ModelResponse(text="半段", backend="fake", truncated=True)}[reason]
    attempts, covered = {}, []

    def generate(candidate):
        assert budget._request_tokens(candidate) <= 1472
        start, end, _, part = segment_part(candidate.prompt)
        attempts[start] = attempts.get(start, 0) + 1
        if attempts[start] == 1:
            return invalid
        covered.append((start, end, part))
        return ModelResponse(text="完整事实摘要", backend="fake")

    request = _request("全段Unicode🪴与路径/参数" * 1200)
    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    budget.generate_bounded_compact_response(request, preserve_complete_fallback=True)
    assert "".join(part for _, _, part in covered) == json.dumps(request.messages, ensure_ascii=False)
    assert all(a[1] == b[0] for a, b in zip(covered, covered[1:]))
    assert max(attempts.values()) == 2


def test_many_message_summary_avoids_whole_json_copy(monkeypatch):
    request = _request("")
    request.agent.config.model_context_window_tokens = 20_000
    request = replace(request, messages=[{"role": "user", "content": [{"type": "text", "text": f"行{i}:" + "x" * 2000}]} for i in range(1200)])
    expected = json.dumps(request.messages, ensure_ascii=False)
    digest = hashlib.sha256()
    chars, calls = 0, 0

    def generate(candidate):
        nonlocal chars, calls
        start, end, total, part = segment_part(candidate.prompt)
        assert start == chars and total == len(expected)
        assert end - start == len(part)
        digest.update(part.encode("utf-8"))
        chars, calls = end, calls + 1
        return ModelResponse(text="保留每段事实", backend="fake")

    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    tracemalloc.start()
    try:
        budget.generate_bounded_compact_response(request, preserve_complete_fallback=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert chars == len(expected) and calls > 2
    assert digest.digest() == hashlib.sha256(expected.encode("utf-8")).digest()
    assert peak < len(expected) // 2, (peak, len(expected))


def test_source_mutation_during_summary_never_returns_covered_result(monkeypatch):
    request = replace(_request(""), messages=[{"role": "user", "content": "x" * 10000} for _ in range(5)])
    calls = []

    def generate(_candidate):
        calls.append(True)
        request.messages[-1]["content"] = "y" * 10000
        return ModelResponse(text="候选摘要", backend="fake")

    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    with pytest.raises(ConversationCompactError) as error:
        budget.generate_bounded_compact_response(request, preserve_complete_fallback=True)
    assert calls and error.value.code == "COMPACT_SOURCE_CHANGED"


def test_cancellation_at_second_scan_eof_is_not_lost():
    scans, cancelled = 0, False

    def replay():
        nonlocal scans, cancelled
        scans += 1
        yield "abc"
        if scans == 2:
            cancelled = True

    with CompactTextSource(replay, lambda: cancelled) as source:
        assert source[0:3] == "abc"
        source.discard_before(3)
        with pytest.raises(InterruptedError):
            source.finish()


def test_measure_encode_failure_explicitly_closes_generator():
    closed = []

    def replay():
        try:
            yield "\ud800"
        finally:
            closed.append(True)

    with pytest.raises(UnicodeEncodeError):
        CompactTextSource(replay)
    assert closed == [True]


@pytest.mark.parametrize("cancel_stage", ["estimate", "response"])
def test_fitting_summary_honors_cancellation_after_estimate_or_reply(monkeypatch, cancel_stage):
    cancelled, calls = False, []
    original = budget._request_tokens

    def estimate(request, source=None):
        nonlocal cancelled
        tokens = original(request, source)
        if cancel_stage == "estimate":
            cancelled = True
        return tokens

    def generate(_request):
        nonlocal cancelled
        calls.append(True)
        cancelled = True
        return ModelResponse(text="迟到摘要", backend="fake")

    monkeypatch.setattr(budget, "_request_tokens", estimate)
    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    with pytest.raises(InterruptedError):
        budget.generate_bounded_compact_response(_request("很短的材料"), interrupt_check=lambda: cancelled)
    assert len(calls) == (0 if cancel_stage == "estimate" else 1)
