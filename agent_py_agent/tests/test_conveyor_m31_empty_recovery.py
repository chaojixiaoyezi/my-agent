from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core import provider_transient_auto_resume as transient
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderResponseError
from agent_py_agent.agent.conversation import compact_request_budget as compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.tests.test_compact_request_budget import _request


def test_cached_empty_changes_the_second_payload(monkeypatch):
    sent = []

    def generate(request):
        sent.append(request)
        return ModelResponse("" if len(sent) == 1 else "summary", "fixture")

    monkeypatch.setattr(compact, "generate_auxiliary_model_response", generate)
    assert compact.generate_bounded_compact_response(_request("history")).text == "summary"
    assert len(sent) == 2 and sent[0].prompt != sent[1].prompt
    assert sent[0].messages == sent[1].messages


def test_segment_dispatch_cannot_exceed_the_shared_attempt_budget(monkeypatch):
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
    with pytest.raises(ConversationCompactError) as failure:
        compact.generate_bounded_compact_response(request)
    assert failure.value.code == "COMPACT_EMPTY_BUDGET_EXHAUSTED"
    assert len(sent) == 3 + compact._SEGMENT_REPAIR_LIMIT_COUNT


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
    from agent_py_agent.tests.test_compact_request_budget import segment_part

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
