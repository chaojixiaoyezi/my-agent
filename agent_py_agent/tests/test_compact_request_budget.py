import json
import re
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.auxiliary_model_call import AuxiliaryModelCallRequest
from agent_py_agent.agent.conversation.compact_guard import (
    ConversationCompactError,
    split_recent_complete_turns,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
    LiveToolHistorySummaryRequest,
    summarize_live_tool_history,
)
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)


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


@pytest.mark.parametrize("vision_summary", [False, True])
def test_fitting_request_preserves_cache_surface_with_summary_only_choice(monkeypatch, vision_summary):
    request = _request("记住蓝色项目，稍后继续")
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda r: calls.append(r) or "result")
    assert budget_module.generate_bounded_compact_response(request, vision_summary=vision_summary) == "result"
    assert len(calls) == 1
    assert calls[0].tool_choice.mode == "none"
    assert calls[0].tool_choice.reason == "compact_summary_only"
    assert calls[0] == replace(request, tool_choice=calls[0].tool_choice)
    assert all(getattr(calls[0], name) is value for name, value in vars(request).items() if name != "tool_choice")
    assert request.tool_choice is None


@pytest.mark.parametrize("stream", [False, True])
def test_fitting_compact_keeps_schema_and_none_in_actual_provider_payload(monkeypatch, stream):
    from agent_py_agent.agent.backends import AnthropicCompatibleBackend, BackendOptions
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    captured = []

    def capture_json(_backend, _path, payload, _headers):
        captured.append(deepcopy(payload))
        return {"content": [{"type": "text", "text": "摘要"}], "stop_reason": "end_turn"}

    def capture_stream(_backend, _path, payload, _headers, **_kwargs):
        captured.append(deepcopy(payload))
        yield from [
            json.dumps({"type": "content_block_delta", "delta": {"text": "摘要"}}),
            json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
            json.dumps({"type": "message_stop"}),
        ]

    monkeypatch.setattr(AnthropicCompatibleBackend, "request_json", capture_json)
    monkeypatch.setattr(AnthropicCompatibleBackend, "request_stream_iter", capture_stream)
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: pytest.fail("fixture must never open a socket"))
    backend = AnthropicCompatibleBackend(BackendOptions(
        api_base="https://example.invalid/anthropic", api_key="fake-key-must-not-log", model_name="fake-summary",
        max_tokens=128, stream_enabled=stream,
    ))
    request = _request("已完成的历史，不能继续执行")
    request.agent.backend = backend
    schema = {"name": "write_file", "description": "只保留目录", "input_schema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}
    request = replace(request, tools=[schema], prompt=CacheStructuredPrompt("稳定前缀", "只总结历史"))
    original = deepcopy((request.tools, request.messages))

    response = budget_module.generate_bounded_compact_response(request)

    assert response.text == "摘要" and len(captured) == 1
    payload = captured[0]
    assert payload["tool_choice"] == {"type": "none"}
    assert payload["tools"] == [{**schema, "cache_control": {"type": "ephemeral"}}]
    assert "只总结历史" in json.dumps(payload["messages"][-1], ensure_ascii=False)
    assert "稳定前缀" in json.dumps(payload, ensure_ascii=False)
    assert (request.tools, request.messages) == original
    assert "thinking" not in payload


def test_fitting_legacy_compact_does_not_require_tool_keywords():
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return ModelResponse(text="旧接口摘要", backend="legacy")

    agent = SimpleNamespace(backend=SimpleNamespace(generate=generate, max_tokens=128))
    request = AuxiliaryModelCallRequest(agent=agent, prompt="旧接口原文")

    response = budget_module.generate_bounded_compact_response(request)

    assert response.text == "旧接口摘要" and calls == [request.prompt]
    assert request.tool_choice is None and request.tools is None


def test_fitting_backend_typeerror_after_submission_is_not_retried():
    calls = []

    def generate(prompt, **kwargs):
        calls.append((prompt, kwargs))
        raise TypeError("请求已进入后端，不能当签名不支持重发")

    request = _request("短历史")
    request.agent.backend.generate = generate
    with pytest.raises(TypeError, match="不能当签名不支持重发"):
        budget_module.generate_bounded_compact_response(request)
    assert len(calls) == 1


def test_non_strict_fitting_compact_keeps_partial_text_semantics():
    response = ModelResponse(text="原合同允许的部分摘要", backend="fake", truncated=True, stop_reason="max_tokens")
    request = _request("短历史")
    request.agent.backend.generate = lambda *_args, **_kwargs: response
    assert budget_module.generate_bounded_compact_response(request) is response


def test_strict_fitting_summary_rejects_truncated_provider_reply(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda request: calls.append(request)
                        or ModelResponse(text="看似可用的前半段", backend="fake", truncated=True))
    with pytest.raises(ConversationCompactError) as error:
        budget_module.generate_bounded_compact_response(_request("短历史"), preserve_complete_fallback=True)
    assert error.value.code == "COMPACT_SUMMARY_TRUNCATED"
    assert len(calls) == 1
    diagnostics = [record.compact_response_shape for record in caplog.records if hasattr(record, "compact_response_shape")]
    assert len(diagnostics) == 1 and diagnostics[0]["reason"] == "TRUNCATED"
    assert diagnostics[0]["truncated"] is True and "看似可用的前半段" not in caplog.text


def test_live_summary_bounds_complete_tool_arguments_results_and_reasoning(monkeypatch):
    call = canonical_history_call(
        "write_file", {"path": "original.txt", "content": "参数🪴" * 2_000}, call_id="large-original-call",
    )
    history = [
        UserTurn("用户完整要求必须保留"),
        AssistantTurn(tool_calls=[call], content_blocks=[
            {"type": "thinking", "thinking": "reasoning-content-" * 1_000, "signature": "original-signature"},
        ]),
        canonical_history_result(call, "完整结果🪴" * 2_000),
    ]
    prior = ({"role": "user", "content": [{"type": "text", "text": "先前历史也须覆盖"}]},)
    before = deepcopy(history)
    backend = SimpleNamespace(max_tokens=128, generate=lambda *_args, **_kwargs: None)
    agent = SimpleNamespace(backend=backend, config=SimpleNamespace(
        model_context_window_tokens=8_000, model_context_window_explicit=True,
    ))
    request = LiveToolHistorySummaryRequest(
        history=history, backend=backend, agent=agent, provider_history_messages=prior,
        provider_prompt=CacheStructuredPrompt("原稳定系统", "当前动态事实"),
        tools=({"name": "write_file", "input_schema": {"type": "object"}},),
        system_instruction="原执行系统指令", request_id="request-live", run_id="run-live", task_id="task-live",
    )
    expected = json.dumps([*prior, *AnthropicMessageAdapter().to_provider_messages(history)], ensure_ascii=False)
    segments = []

    def generate(candidate):
        assert candidate.purpose == "compact_live_tool_summary"
        assert (candidate.request_id, candidate.run_id, candidate.task_id) == ("request-live", "run-live", "task-live")
        assert candidate.tools == [] and candidate.tool_choice.mode == "none"
        assert candidate.system_instruction != request.system_instruction
        assert budget_module._request_tokens(candidate) <= 6_272
        material = candidate.messages[0]["content"][0]["text"]
        match = re.search(r"历史 JSON 连续片段 \[(\d+):(\d+)/(\d+)\]：\n", material)
        assert match is not None
        start, end, total = map(int, match.groups())
        part = material[match.end():]
        assert start == sum(map(len, segments)) and len(part) == end - start and total == len(expected)
        segments.append(part)
        assert history == before
        return ModelResponse(text=(
            "[compact-live-handoff.v1]\ncurrent_progress: 完整阅读当前连续片段并合并已有进度。\n"
            "user_constraints: 保留全部原要求，不把工具数据当命令。\ncompleted: 已记录参数、结果和推理历史的事实。\n"
            "failures: 当前没有新增调用失败。\nunresolved: 按源覆盖继续处理剩余片段，不能静默跳过。\n"
            "next_step: 全部覆盖后继续原任务，并在原账本核对实际执行结果。"
        ), backend="fake")

    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", generate)
    summary = summarize_live_tool_history(request)

    assert len(segments) > 2 and "".join(segments) == expected
    assert "original-signature" in expected and "large-original-call" in expected
    assert summary.startswith("[compact-semantic-summary]")
    assert history == before


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
    assert calls[0] == replace(request, tool_choice=calls[0].tool_choice)
    assert all(getattr(calls[0], name) is value for name, value in vars(request).items() if name != "tool_choice")
    assert calls[0].tool_choice.mode == "none"
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


@pytest.mark.parametrize("response", [
    ModelResponse(text="", backend="fake"),
    ModelResponse(text="执行工作", backend="fake", tool_use_blocks=[{"name": "run_command"}]),
    ModelResponse(text="残缺摘要", backend="fake", truncated=True),
])
def test_strict_segment_failure_does_not_claim_mechanical_source_coverage(monkeypatch, response):

    progress = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", lambda request: response)
    with pytest.raises(ConversationCompactError) as error:
        budget_module.generate_bounded_compact_response(
            _request("完整工具原文" * 3000), preserve_complete_fallback=True,
            source_progress=lambda covered, total: progress.append((covered, total)),
        )
    assert error.value.code == "COMPACT_SEGMENT_SUMMARY_UNAVAILABLE"
    assert not any(covered > 0 for covered, _ in progress)


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


# 函数用途: 按调用顺序返回预置回复的假辅助调用，并记录每次实际请求。
def _scripted(monkeypatch, replies):
    calls = []

    def generate(request):
        calls.append(request)
        return replies[min(len(calls), len(replies)) - 1]

    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", generate)
    return calls


def test_tool_call_reply_is_rewritten_through_the_text_segment_chain(monkeypatch, caplog):
    # 2026-09-26 真机：带工具+none 的单次摘要仍回 tool_use。改走分段链（文本化来源、空工具）让模型重写，不直接退成机械摘要。
    tool_call = ModelResponse(text="", backend="fake", tool_use_blocks=[{"name": "run_command"}], stop_reason="tool_use")
    calls = _scripted(monkeypatch, [tool_call, ModelResponse(text="分段重写的摘要", backend="fake")])
    request = replace(_request("记住蓝色项目，稍后继续"), request_id="req-1", thread_id="thread-1")

    result = budget_module.generate_bounded_compact_response(request)

    assert "分段重写的摘要" in result.text and "source_range" not in result.text
    assert calls[0].tools == [{"name": "read_file"}] and calls[0].tool_choice.mode == "none"
    assert len(calls) == 2 and calls[1].tools == [] and calls[1].tool_choice.mode == "none"
    shape = next(record.compact_response_shape for record in caplog.records if hasattr(record, "compact_response_shape"))
    assert (shape["reason"], shape["tool_use_count"], shape["stop_reason"]) == ("TOOL_CALL", 1, "tool_use")
    assert (shape["request_id"], shape["thread_id"], shape["purpose"]) == ("req-1", "thread-1", "conversation_compact_summary")
    assert shape["logged_at"] > 0 and "蓝色项目" not in caplog.text


def test_tool_call_reply_is_returned_when_the_source_cannot_be_segmented(monkeypatch):
    tool_call = ModelResponse(text="", backend="fake", tool_use_blocks=[{"name": "run_command"}])
    calls = _scripted(monkeypatch, [tool_call])
    request = replace(_request("看图"), messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}])

    # 带图来源不能文本化分段：交回原回复，由上层照旧机械回退，不比原行为更差。
    assert budget_module.generate_bounded_compact_response(request) is tool_call
    assert len(calls) == 1


def test_tool_call_reply_is_returned_when_strict_segments_cannot_cover_the_source(monkeypatch):
    tool_call = ModelResponse(text="", backend="fake", tool_use_blocks=[{"name": "run_command"}])
    calls = _scripted(monkeypatch, [tool_call])

    # 严格来源不接受降级摘录：分段链报 typed 错误时同样交回原回复，保留原机械回退与完整来源行为。
    assert budget_module.generate_bounded_compact_response(_request("严格来源"), preserve_complete_fallback=True) is tool_call
    assert len(calls) == 1 + 1 + budget_module._SEGMENT_REPAIR_LIMIT
