import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core import provider_transient_auto_resume as transient
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import (
    ProviderContextWindowError,
    ProviderResponseError,
    ProviderTransientError,
)
from agent_py_agent.agent.conversation import compact_request_budget as compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.tests.test_compact_request_budget import _request, segment_part


def test_cached_empty_changes_the_second_payload(monkeypatch):
    sent = []

    def generate(request):
        sent.append(request)
        return ModelResponse("" if len(sent) == 1 else "summary", "fixture")

    monkeypatch.setattr(compact, "generate_auxiliary_model_response", generate)
    assert compact.generate_bounded_compact_response(_request("history")).text == "summary"
    assert len(sent) == 2 and sent[0].prompt != sent[1].prompt
    assert sent[0].messages == sent[1].messages


def _created_budgets(monkeypatch):
    created = []
    original = compact._new_compact_attempt_budget

    def create(agent):
        created.append(original(agent))
        return created[-1]

    monkeypatch.setattr(compact, "_new_compact_attempt_budget", create)
    return created


# 4.8 MB 普通文本来源，默认输出预留；窗口取 131,072 或 200,000 token。
def _large_request(window, size=4_800_000):
    line = "ordinary project log line with numbers 12345 and a path /srv/app/module.py\n"
    agent = SimpleNamespace(backend=SimpleNamespace(max_tokens=16_384),
                            config=SimpleNamespace(model_context_window_tokens=window))
    return replace(_request(""), agent=agent,
                   messages=[{"role": "user", "content": [{"type": "text", "text": line * (size // len(line))}]}])


def _assert_contiguous(prompts, total):
    ranges = [segment_part(prompt)[:3] for prompt in prompts]
    assert ranges[0][0] == 0 and ranges[-1][1] == total and {extent for *_, extent in ranges} == {total}
    assert all(end == start for (_, end, _), (start, _, _) in zip(ranges, ranges[1:]))


def test_one_char_segments_draw_only_source_credits(monkeypatch):
    sent = []

    def generate(request):
        sent.append(request)
        if request.messages is not None:
            raise ProviderContextWindowError("simulated smaller window")
        return ModelResponse("summary", "fixture")

    monkeypatch.setattr(transient, "provider_transient_retry_delays", lambda _policy: ())
    monkeypatch.setattr(compact, "generate_auxiliary_model_response", generate)
    monkeypatch.setattr(compact, "_segment_end", lambda _base, _src, offset, *_, **_options: offset + 1)
    request = replace(_request("history"), tools=[])
    created = _created_budgets(monkeypatch)
    assert compact.generate_bounded_compact_response(request).text == "summary"
    total = len(json.dumps(request.messages, ensure_ascii=False))
    [budget] = created
    # 退化几何下每个字符各成一段：首发额度只按严格前进授予，重试配额只扣了那次缓存发送。
    assert total > 2 + compact._SEGMENT_REPAIR_LIMIT_COUNT and budget.remaining == 1 + compact._SEGMENT_REPAIR_LIMIT_COUNT
    assert budget.consumed_by_stage == {"cached": 1, "segment": total} and budget.source_frontier == total - 1
    _assert_contiguous([item.prompt for item in sent[1:]], total)
    assert budget.grant_source_segment(total - 1, total) is False and budget.source_grant_pending is False


@pytest.mark.parametrize("window", [131_072, 200_000])
def test_healthy_segments_cover_a_large_source_in_one_attempt(monkeypatch, window):
    request = _large_request(window)
    initial = compact._new_compact_attempt_budget(request.agent).remaining
    created = _created_budgets(monkeypatch)
    sent = []
    monkeypatch.setattr(compact, "generate_auxiliary_model_response",
                        lambda item: sent.append(item) or ModelResponse(f"summary {len(sent)}", "fixture"))
    response = compact.generate_bounded_compact_response(request)
    [budget] = created
    assert len(sent) > initial and response.text == f"summary {len(sent)}"
    assert budget.remaining == initial and budget.consumed_by_stage == {"segment": len(sent)}
    _assert_contiguous([item.prompt for item in sent], len(json.dumps(request.messages, ensure_ascii=False)))


def test_default_path_fixture_completes_with_the_default_budget(monkeypatch):
    request = _request("中间也必须保留🪴\n" * 3000)
    initial = compact._new_compact_attempt_budget(request.agent).remaining
    sent = []
    monkeypatch.setattr(compact, "generate_auxiliary_model_response",
                        lambda item: sent.append(item) or ModelResponse("保留先前事实与本段的新事实", "fixture"))
    assert compact.generate_bounded_compact_response(request).text == "保留先前事实与本段的新事实"
    assert len(sent) > initial
    _assert_contiguous([item.prompt for item in sent], len(json.dumps(request.messages, ensure_ascii=False)))


def test_transport_retries_spend_the_allowance_while_segments_progress(monkeypatch):
    waits, sent = [], []

    def generate(item):
        sent.append(item)
        if len(sent) % 2:
            raise ProviderTransientError("simulated I/O error")
        return ModelResponse("summary", "fixture")

    monkeypatch.setattr(transient, "provider_transient_retry_delays", lambda _policy: (1.0, 1.0))
    monkeypatch.setattr(transient, "wait_interruptibly", waits.append)
    monkeypatch.setattr(compact, "generate_auxiliary_model_response", generate)
    request = _large_request(131_072)
    created = _created_budgets(monkeypatch)
    with pytest.raises(ConversationCompactError) as failure:
        compact.generate_bounded_compact_response(request)
    [budget] = created
    allowance = 2 + compact._SEGMENT_REPAIR_LIMIT_COUNT + 2
    assert failure.value.code == "COMPACT_EMPTY_BUDGET_EXHAUSTED" and budget.exhausted_cause == "transient"
    # 每段推进都不回补配额：六次重试全部来自初始配额，第七段首发失败后不再重发。
    assert budget.remaining == 0 and budget.consumed_by_stage == {"segment": allowance + 1, "transient": allowance}
    assert len(sent) == 2 * allowance + 1 and len(waits) == allowance
    firsts, retries = sent[0::2], sent[1::2]
    assert [item.prompt for item in firsts[:-1]] == [item.prompt for item in retries]
    ranges = [segment_part(item.prompt)[:2] for item in firsts]
    assert ranges[0][0] == 0 and all(end == start for (_, end), (start, _) in zip(ranges, ranges[1:]))


def test_empty_repairs_spend_the_allowance_and_progress_never_refills_it(monkeypatch):
    sent = []

    def generate(item):
        sent.append(item)
        return ModelResponse("summary" if "纠正要求" in item.prompt else "", "fixture")

    monkeypatch.setattr(transient, "provider_transient_retry_delays", lambda _policy: ())
    monkeypatch.setattr(compact, "generate_auxiliary_model_response", generate)
    request = _large_request(131_072)
    created = _created_budgets(monkeypatch)
    with pytest.raises(ConversationCompactError) as failure:
        compact.generate_bounded_compact_response(request)
    [budget] = created
    allowance = 2 + compact._SEGMENT_REPAIR_LIMIT_COUNT
    assert failure.value.code == "COMPACT_EMPTY_BUDGET_EXHAUSTED" and budget.exhausted_cause == "segment_repair"
    assert budget.consumed_by_stage == {"segment": allowance + 1, "segment_repair": allowance}
    assert budget.empty_count == allowance + 1 and len(sent) == 2 * allowance + 1 and budget.remaining == 0
    assert all(first.prompt != repair.prompt for first, repair in zip(sent[0::2], sent[1::2]))


def test_cancellation_stops_a_long_healthy_attempt_between_segments(monkeypatch):
    request = _large_request(131_072)
    initial = compact._new_compact_attempt_budget(request.agent).remaining
    sent = []
    monkeypatch.setattr(compact, "generate_auxiliary_model_response",
                        lambda item: sent.append(item) or ModelResponse("summary", "fixture"))
    with pytest.raises(InterruptedError):
        compact.generate_bounded_compact_response(request, interrupt_check=lambda: len(sent) > initial)
    assert len(sent) == initial + 1


def test_zero_tool_first_turn_can_repair_one_empty():
    params = SimpleNamespace(executed_tools=[], live_archive_state={})
    empty = ProviderResponseError("empty", error_code="MODEL_EMPTY_RESPONSE")
    assert loop._should_retry_empty_model_response(params, empty, 0)
    assert not loop._should_retry_empty_model_response(params, empty, 1)


@pytest.mark.parametrize("transport", ["http", "ws"])
def test_transport_retries_spend_the_same_budget(monkeypatch, caplog, transport):
    import logging
    from urllib.error import URLError

    from agent_py_agent.agent.backends import gateway_helpers as gateway
    from agent_py_agent.agent.backends import responses_websocket as ws
    from agent_py_agent.tests.test_responses_websocket import request as websocket_request

    sent = []
    budget = compact.CompactAttemptBudget(2, {})
    envelope = gateway.GatewayRequest("https://example.invalid", "test-key", "/chat/completions", {}, {}, 1)

    def open_request(*args):
        sent.append(args)
        raise URLError(ConnectionRefusedError("simulated I/O error"))

    if transport == "ws":
        monkeypatch.setattr(ws, "_connect_admitted", open_request)
        monkeypatch.setattr(ws, "_request_retry_wait", lambda *_: None)
        monkeypatch.setattr(compact, "generate_auxiliary_model_response",
                            lambda _request: tuple(ws.iter_responses_websocket(websocket_request())))
    else:
        monkeypatch.setattr(gateway, "_gateway_urlopen", open_request)
        monkeypatch.setattr(gateway, "_request_retry_wait", lambda *_: None)
        monkeypatch.setattr(compact, "generate_auxiliary_model_response", lambda _request: gateway.post_json(envelope))
    monkeypatch.setattr(compact, "_new_compact_attempt_budget", lambda _agent: budget)

    with caplog.at_level(logging.INFO), pytest.raises(ConversationCompactError) as failure:
        compact.generate_bounded_compact_response(_request("history"))
    assert failure.value.code == "COMPACT_EMPTY_BUDGET_EXHAUSTED"
    assert len(sent) == 2
    assert budget.remaining == 0 and budget.consumed_by_stage == {"cached": 1, "transient": 1}
    facts = [record.compact_request_attempt for record in caplog.records if hasattr(record, "compact_request_attempt")]
    assert len({row["attempt_id"] for row in facts}) == len(facts) == 2
    assert all(row["attempt_id"].startswith(f"provider-{transport}:") for row in facts)
    assert all(row["prompt_tokens"] is row["output_tokens"] is row["reasoning_tokens"] is None for row in facts)
    assert all(row["stream_end_frame"] is None and row["source_range"] == ("messages", 0, 1) for row in facts)


def test_cached_tool_shape_forces_no_tool_choice(monkeypatch):
    sent = []

    def generate(request):
        sent.append(request)
        if len(sent) == 1:
            return ModelResponse("", "fixture", tool_use_blocks=[{"name": "read_file", "input": {}}])
        return ModelResponse("summary", "fixture")

    monkeypatch.setattr(compact, "generate_auxiliary_model_response", generate)
    response = compact.generate_bounded_compact_response(_request("history"))
    assert response.text == "summary" and response.tool_use_blocks == []
    assert [item.tool_choice.mode for item in sent] == ["auto", "none"]
    assert sent[0].messages == sent[1].messages
    assert sent[0].tools == sent[1].tools


def test_repeated_segment_empty_changes_payload_or_stops(monkeypatch):
    sent = []

    def generate(request):
        sent.append(request)
        if request.messages is not None:
            raise ProviderContextWindowError("simulated smaller window")
        return ModelResponse("", "fixture")

    monkeypatch.setattr(compact, "generate_auxiliary_model_response", generate)
    request = _request("some source that can be narrowed")
    with pytest.raises(ConversationCompactError) as failure:
        compact.generate_bounded_compact_response(request)
    assert failure.value.code == "COMPACT_EMPTY_BUDGET_EXHAUSTED"
    prompts = [item.prompt for item in sent[1:]]
    assert len(prompts) == len(set(prompts)) == 3
    original, repair, narrower = [segment_part(prompt) for prompt in prompts]
    assert original[:3] == repair[:3] and narrower[0] == original[0] < narrower[1] < original[1]


def test_exhaustion_preserves_committed_checkpoint_and_resume_cursor(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import BackendOptions
    from agent_py_agent.agent.backends.http import HttpBackend
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                         max_tokens=64, model_context_window_tokens=10_000)
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)
    agent.backend = HttpBackend(BackendOptions(
        "https://example.invalid", "test-key", "model", context_window_tokens=10_000, max_tokens=4_000,
    ))
    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "m31-checkpoint"})
    for index in range(3):
        for role in ("user", "assistant"):
            store.messages.append({"thread_id": thread.thread_id, "role": role, "content": f"old turn {index}",
                                   "metadata": {"conversation_request_id": f"old-{index}"}})
    options = ConversationCompactOptions(
        force=True, request_projector=lambda view: ConversationCompactProjection(200 if view.is_candidate else 5000, object())
    )
    monkeypatch.setattr(compact, "generate_auxiliary_model_response", lambda _req: ModelResponse("prior summary", "fixture"))
    prepare_conversation_context(agent, store, thread, options=options)
    saved = store.threads.require(thread.thread_id)
    assert saved.compact_generation > 0 and saved.compact_checkpoint_id
    for index in range(3):
        for role in ("user", "assistant"):
            store.messages.append({"thread_id": thread.thread_id, "role": role, "content": f"new turn {index}",
                                   "metadata": {"conversation_request_id": f"new-{index}"}})

    sent = []

    def empty(request):
        sent.append(request)
        return ModelResponse("", "fixture")

    monkeypatch.setattr(compact, "generate_auxiliary_model_response", empty)
    monkeypatch.setattr(compact, "_new_compact_attempt_budget", lambda _agent: compact.CompactAttemptBudget(2, {}))
    with pytest.raises(ConversationCompactError) as failure:
        prepare_conversation_context(agent, store, saved, options=options)
    assert failure.value.code == "COMPACT_EMPTY_BUDGET_EXHAUSTED" and len(sent) == 2
    after_failure = store.threads.require(thread.thread_id)
    assert after_failure.compact_generation == saved.compact_generation
    assert after_failure.compact_checkpoint_id == saved.compact_checkpoint_id
    assert after_failure.compacted_through_message_id == saved.compacted_through_message_id
    assert after_failure.compacted_through_byte_offset == saved.compacted_through_byte_offset

    monkeypatch.setattr(compact, "generate_auxiliary_model_response", lambda _req: ModelResponse("new summary", "fixture"))
    prepare_conversation_context(agent, store, after_failure, options=options)
    assert store.threads.require(thread.thread_id).compact_generation == saved.compact_generation + 1


def test_large_store_history_commits_one_checkpoint_after_many_segments(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import BackendOptions
    from agent_py_agent.agent.backends.http import HttpBackend
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                         max_tokens=64, model_context_window_tokens=131_072)
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)
    agent.backend = HttpBackend(BackendOptions(
        "https://example.invalid", "test-key", "model", context_window_tokens=131_072, max_tokens=16_384,
    ))
    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "m31-large-source"})
    # 约 5.1 MB 历史：默认几何下需要的有效片段数超过固定重试配额。
    for index in range(160):
        store.messages.append({"thread_id": thread.thread_id, "role": "assistant" if index % 2 else "user",
                               "content": f"record {index:06d} " + "x" * 32_000,
                               "metadata": {"conversation_request_id": f"large-{index // 2}"}})
    before = store.threads.require(thread.thread_id)
    options = ConversationCompactOptions(
        force=True, request_projector=lambda view: ConversationCompactProjection(200 if view.is_candidate else 5000, object())
    )
    initial = compact._new_compact_attempt_budget(agent).remaining
    sent = []

    def stalled(request):
        sent.append(request)
        return ModelResponse("summary" if len(sent) <= initial + 2 else "", "fixture")

    monkeypatch.setattr(compact, "generate_auxiliary_model_response", stalled)
    with pytest.raises(ConversationCompactError) as failure:
        prepare_conversation_context(agent, store, before, options=options)
    assert failure.value.code == "COMPACT_EMPTY_BUDGET_EXHAUSTED" and len(sent) > initial + 2
    after_failure = store.threads.require(thread.thread_id)
    assert after_failure.compact_generation == before.compact_generation == 0
    assert after_failure.compact_checkpoint_id == before.compact_checkpoint_id
    assert after_failure.compacted_through_message_id == before.compacted_through_message_id
    assert after_failure.compacted_through_byte_offset == before.compacted_through_byte_offset

    sent.clear()
    monkeypatch.setattr(compact, "generate_auxiliary_model_response",
                        lambda request: sent.append(request) or ModelResponse(f"summary {len(sent)}", "fixture"))
    prepare_conversation_context(agent, store, after_failure, options=options)
    saved = store.threads.require(thread.thread_id)
    assert len(sent) > initial and saved.compact_generation == 1 and saved.compact_source_messages == 160
    _assert_contiguous([request.prompt for request in sent], segment_part(sent[0].prompt)[2])


@pytest.mark.parametrize("shape", ["visible_text", "complete_tool_block", "reasoning_only", "stream_incomplete"])
def test_nonempty_response_shapes_do_not_get_empty_repair(shape):
    params = SimpleNamespace(executed_tools=[], live_archive_state={})
    response_error = ProviderResponseError("result", error_code="MODEL_EMPTY_RESPONSE", details={"response_shape": shape})
    assert not loop._should_retry_empty_model_response(params, response_error, 0)


def test_already_displayed_output_and_cancel_preempt_empty_repair():
    empty = ProviderResponseError("empty", error_code="MODEL_EMPTY_RESPONSE")
    params = SimpleNamespace(executed_tools=[], live_archive_state={"_model_output_displayed": True})
    assert not loop._should_retry_empty_model_response(params, empty, 0)
    params.live_archive_state.clear()
    params.cancellation_token = SimpleNamespace(cancelled=True)
    assert not loop._should_retry_empty_model_response(params, empty, 0)


def test_chat_records_terminal_shape_and_reported_only_counts(monkeypatch):
    import json

    from agent_py_agent.agent.backends import BackendOptions
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend

    backend = OpenAICompatibleBackend(BackendOptions("https://example.invalid", "test-key", "model", stream_enabled=True))
    lines = [
        json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        json.dumps({"choices": [], "usage": {"prompt_tokens": 13}}),
        "[DONE]",
    ]
    monkeypatch.setattr(backend, "request_stream", lambda *_args: lines)
    with pytest.raises(ProviderResponseError) as failure:
        backend.generate("history", tools=[{"name": "read_file", "input_schema": {"type": "object"}}])
    assert failure.value.error_code == "MODEL_EMPTY_RESPONSE"
    assert failure.value.details == {
        "response_shape": "empty_final", "text_chars": 0, "tool_use_count": 0,
        "stop_reason": "stop", "stream_end_frame": True,
        "prompt_tokens": 13, "output_tokens": None, "reasoning_tokens": None,
    }


def test_first_turn_reuses_params_and_reports_both_empty_attempts(monkeypatch):
    from agent_py_agent.agent.agent_core import tool_model_generation

    params = SimpleNamespace(executed_tools=[], tool_context=[], effective_on_chunk=None,
                             run_id="run-1", live_archive_state={})
    agent = SimpleNamespace(runtime_guard_policy=None)
    seen = []

    def sample(_agent, passed, rounds):
        assert passed is params and rounds == 0
        turn_id = tool_model_generation._begin_model_turn_identity(passed, rounds)
        seen.append((turn_id, tuple(passed.tool_context)))
        raise ProviderResponseError("empty", error_code="MODEL_EMPTY_RESPONSE",
                                    details={"text_chars": 0, "stream_end_frame": True})

    monkeypatch.setattr(loop, "stale_subagent_attempt_message", lambda *_: None)
    monkeypatch.setattr(loop, "begin_goal_model_turn", lambda *_: None)
    monkeypatch.setattr(loop, "has_pending_turn_input", lambda *_: False)
    monkeypatch.setattr(loop, "build_tool_loop_prompt", lambda *_: "same prompt")
    monkeypatch.setattr(loop, "next_tool_loop_model_response", sample)
    repair = loop._model_turn_or_retry(agent, params, 0, 0)
    assert repair.params is params and repair.retry_after_provider_response
    with pytest.raises(ProviderResponseError) as failure:
        loop._model_turn_or_retry(agent, repair.params, 0, repair.provider_response_repairs)
    assert failure.value.details["side_effect_state"] == "not_started"
    assert [item["response_shape"] for item in failure.value.details["attempts"]] == ["empty_final"] * 2
    assert seen[0][0] == seen[1][0] and seen[0][1] == () and len(seen[1][1]) == 1
    assert params.tool_context == [] and params.live_archive_state["_model_turn_sequence"] == 2


def test_steer_replaces_a_scheduled_empty_repair(monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop.model_turn import ModelTurnRequest

    params = SimpleNamespace(executed_tools=[], tool_context=[], effective_on_chunk=None, live_archive_state={})
    agent = SimpleNamespace(runtime_guard_policy=None)
    newer_input = False
    seen = []

    def sample(_agent, passed, _rounds):
        seen.append(tuple(passed.tool_context))
        if newer_input:
            return ModelTurnRequest("new input", ModelResponse("new answer", "fixture"), passed)
        raise ProviderResponseError("empty", error_code="MODEL_EMPTY_RESPONSE")

    monkeypatch.setattr(loop, "stale_subagent_attempt_message", lambda *_: None)
    monkeypatch.setattr(loop, "begin_goal_model_turn", lambda *_: None)
    monkeypatch.setattr(loop, "account_goal_model_response", lambda *_: None)
    monkeypatch.setattr(loop, "acknowledge_injected_turn_input", lambda *_: None)
    monkeypatch.setattr(loop, "has_pending_turn_input", lambda *_: newer_input)
    monkeypatch.setattr(loop, "build_tool_loop_prompt", lambda *_: "old input")
    monkeypatch.setattr(loop, "next_tool_loop_model_response", sample)
    repair = loop._model_turn_or_retry(agent, params, 0, 0)
    newer_input = True
    resumed = loop._model_turn_or_retry(agent, repair.params, 0, repair.provider_response_repairs)
    assert resumed.prompt == "new input" and resumed.response.text == "new answer"
    assert seen == [(), ()]


def test_first_turn_chat_repair_reuses_original_user_message(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.backends import BackendOptions
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability

    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl", prompt_files=[]), tmp_path)
    backend = OpenAICompatibleBackend(BackendOptions("https://example.invalid", "test-key", "model", stream_enabled=True))
    agent.backend = backend
    monkeypatch.setattr(backend, "probe_tool_capability", lambda: ProviderToolCapability(
        provider=backend.name, endpoint="local://fixture", model="model", stream=True,
        native_supported=True, evidence="fixture_native"))
    sent = []
    display = []

    def stream(_path, payload, _headers, *args, **options):
        sent.append(payload)
        delta = {} if len(sent) == 1 else {"content": "first turn recovered"}
        return iter([json.dumps({"choices": [{"delta": delta, "finish_reason": "stop"}]}), "[DONE]"])

    monkeypatch.setattr(backend, "request_stream_iter", stream)
    instruction = "Give the status in one sentence."
    result = agent.run(instruction, save=False, allowed_tools=["read_file"], on_chunk=display.append)
    assert result.response == "first turn recovered" and not result.executed_tools
    assert len(sent) == 2 and sent[0]["messages"] != sent[1]["messages"]
    for payload in sent:
        assert json.dumps(payload["messages"], ensure_ascii=False).count(instruction) == 1
    assert "上一轮模型接口返回了空文本" in json.dumps(sent[1]["messages"], ensure_ascii=False)
    assert "".join(display).count("first turn recovered") == 1
