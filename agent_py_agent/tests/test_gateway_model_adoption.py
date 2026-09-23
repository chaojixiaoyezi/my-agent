"""真实 Gateway/PromptBuilder/provider builder 组合，只有 HTTP 为替身，不启动 Gateway 或收费模型。"""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from agent_py_agent.agent import gateway_model_adoption as model_adoption
from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.backends import gateway_helpers, http
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.request_binding import MODEL_OBSERVATION_KEY
from agent_py_agent.agent.model_request_selection import _HOST
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_gateway_model_observation import (
    _optional_admission,  # noqa: F401
    install_backend,
    prepared,
)
from agent_py_agent.tests.test_model_profiles import add


# LLM: 设置/线程选择/请求准备均使用原服务；仅测试部署控制上下文和工具轮上限，未伪造资格或 run 身份。
# 函数用途: 准备 250K 原模型与 1M 候选，并为发送前故障矩阵提供同一个真实入口。
def actual_request(tmp_path, *, mode="apply", tools=False, window=1_000_000, original_window=250_000, candidate_backend="anthropic_compatible"):
    fixture = prepared(tmp_path, mode=mode)
    fixture.agent.config.enable_tools = tools
    fixture.agent.config.stream_enabled = False
    fixture.agent.config.max_tool_rounds = 3
    fixture.agent.config.max_tokens = 4096
    original, _ = add(fixture.agent, model_name="original-model", model_context_window_tokens=original_window)
    model_profiles.execute_model_profile_operation(fixture.agent, "select", {"profile_id": original}, thread_id=fixture.thread_id)
    fixture.original = original
    if window != 1_000_000 or candidate_backend != "anthropic_compatible":
        fixture.candidate, _ = add(fixture.agent, model_name="candidate-large", model_context_window_tokens=window,
                                   model_backend=candidate_backend)
    patch(fixture.agent, {"timeout_seconds": 30, "stage_timeout_seconds": 30})
    return fixture


# LLM: 原 adapters/probe/post_json 调用形状保持；只替换网络端，业务 payload 必须在原投影与最终门之间完全相同。
# 函数用途: 记录实际原生 schema、HTTP 次数与线程选择，并可模拟实际传输之后的错误。
def fake_http(monkeypatch, fixture, *, before_probe=None, on_business=None, use_tool=False):
    business, probes = [], []

    def send(request):
        wire = json.loads(json.dumps(request.payload))
        names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
        native = "system" in wire or any("input_schema" in row for row in wire.get("tools", []))
        tool = None
        gateway_helpers._emit_provider_attempt({"attempt_id": f"http-{len(business) + len(probes)}", "status": "started"})
        if names == ["my_agent_capability_probe"]:
            probes.append(wire)
            if wire["model"] != "original-model" and before_probe:
                before_probe()
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            tool = {"type": "tool_use", "id": "probe", "name": names[0], "input": {"nonce": nonce}}
        else:
            host = _HOST.get()
            thread = fixture.agent.conversation_store.threads.require(fixture.thread_id)
            if wire["model"] == "candidate-large":
                assert host.adoption.submitted
                assert thread.metadata[model_adoption.MODEL_ADOPTION_KEY]["status"] == "send_intent_uncertain"
                if not business:
                    assert wire == host.adoption.expected_payload
            business.append((wire, thread))
            if on_business:
                on_business(wire)
            if use_tool and len(business) == 1:
                tool = {"type": "tool_use", "id": "read-material", "name": "read_file", "input": {"path": str(fixture.agent.root / "material.txt")}}
        if native:
            return {"content": [tool] if tool else [{"type": "text", "text": "资料整理完成。"}],
                    "stop_reason": "tool_use" if tool else "end_turn", "usage": {"input_tokens": 101, "output_tokens": 5}}
        message = {"content": "资料整理完成。"}
        if tool:
            message["tool_calls"] = [{"id": tool["id"], "type": "function", "function": {"name": tool["name"], "arguments": json.dumps(tool["input"])}}]
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}],
                "usage": {"prompt_tokens": 101, "completion_tokens": 5}}

    monkeypatch.setattr(http, "post_json", send)
    return business, probes


@pytest.mark.parametrize("tools", [False, True])
def test_actual_gateway_adopts_only_after_full_payload_and_persists_intent(tmp_path, monkeypatch, tools):
    fixture = actual_request(tmp_path, tools=tools)
    decision = install_backend(monkeypatch, fixture)
    business, probes = fake_http(monkeypatch, fixture)
    before = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    result = request_execution._run_gateway_ask(fixture.context)
    assert result.response == "资料整理完成。"
    assert len(decision.calls) == 1
    assert [row[0]["model"] for row in business] == ["candidate-large"]
    after = business[0][1]
    assert after.model_profile_id == fixture.candidate
    assert after.model_selection_source == "automatic"
    assert after.model_selection_revision == before.model_selection_revision + 1
    assert after.model_selection_last_explicit_revision == before.model_selection_last_explicit_revision
    assert bool(probes) == tools
    assert fixture.context.request[MODEL_OBSERVATION_KEY]["status"] == "send_intent_uncertain"
    assert _HOST.get() is None
    assert fixture.agent.config.model_backend == "echo"
    assert model_call_summary(fixture.agent, request_id=fixture.context.request_id)["purpose_breakdown"]["decision"]["usage_breakdown"]["provider"]["input_tokens"] == 17


@pytest.mark.parametrize("original_window,window", [(250_000, 1_000_000), (1_000_000, 250_000)])
def test_capacity_can_select_up_or_down_without_parent_window_gate(tmp_path, monkeypatch, original_window, window):
    fixture = actual_request(tmp_path, original_window=original_window, window=window)
    install_backend(monkeypatch, fixture)
    business, _ = fake_http(monkeypatch, fixture)
    request_execution._run_gateway_ask(fixture.context)
    assert [row[0]["model"] for row in business] == ["candidate-large"]
    assert business[0][1].metadata[model_adoption.MODEL_ADOPTION_KEY]["validation"]["context_window_tokens"] == window


@pytest.mark.parametrize("tools", [False, True])
def test_none_choice_keeps_original_thinking_options_through_adoption(tmp_path, monkeypatch, tools):
    fixture = actual_request(tmp_path, tools=tools)
    install_backend(monkeypatch, fixture)
    monkeypatch.setattr(
        "agent_py_agent.agent.contracts.required_actions.tool_choice_for_required_actions",
        lambda _snapshot, _tools: ToolChoice.none("host_fixed_choice"),
    )
    business, _ = fake_http(monkeypatch, fixture)
    request_execution._run_gateway_ask(fixture.context)
    assert [row[0]["model"] for row in business] == ["candidate-large"]
    payload = business[0][0]
    assert "tools" not in payload and "tool_choice" not in payload
    assert payload.get("thinking") == ({"type": "disabled"} if tools else None)


def test_tool_rounds_keep_adopted_backend_schema_and_one_decision(tmp_path, monkeypatch):
    fixture = actual_request(tmp_path, tools=True)
    (fixture.agent.root / "material.txt").write_text("需要整理的资料。", encoding="utf-8")
    decision = install_backend(monkeypatch, fixture)
    business, probes = fake_http(monkeypatch, fixture, use_tool=True)
    result = request_execution._run_gateway_ask(fixture.context)
    assert result.response == "资料整理完成。"
    assert [row[0]["model"] for row in business] == ["candidate-large", "candidate-large"]
    assert len(decision.calls) == 1
    assert len(probes) == 2
    assert business[0][0]["tools"] == business[1][0]["tools"]
    assert any(block.get("type") == "tool_result" for row in business[1][0]["messages"] for block in row["content"] if isinstance(block, dict))


# LLM: 故障注入位于真实原发送回调之前，provider observer 已安装；不能直接伪造 adopt 或 bypass 标准事务。
# 函数用途: 为最终核对窗口注入并发更改，证明请求拒绝发生在实际 HTTP 前。
def before_final_check(monkeypatch, action):
    original = model_adoption.GatewayModelAdoption.before_send
    calls = []

    def before(self, backend, prompt, state):
        if self.candidate is not None and not self.submitted and not calls:
            calls.append(state)
            action(self, state)
        return original(self, backend, prompt, state)

    monkeypatch.setattr(model_adoption.GatewayModelAdoption, "before_send", before)
    return calls


@pytest.mark.parametrize("change", ["same_explicit", "off", "credentials", "compact", "deadline", "payload", "runtime"])
def test_final_rejection_has_zero_candidate_http_and_one_original_send(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    fixture = actual_request(tmp_path)
    decision = install_backend(monkeypatch, fixture)
    business, _ = fake_http(monkeypatch, fixture)
    before = fixture.agent.conversation_store.threads.require(fixture.thread_id)

    def mutate(adoption, state):
        if change == "same_explicit":
            model_profiles.execute_model_profile_operation(fixture.agent, "select", {"profile_id": fixture.original}, thread_id=fixture.thread_id)
        elif change == "off":
            patch(fixture.agent, {"points.model_selection.mode": "off"})
        elif change == "credentials":
            data = model_profiles.read_model_profiles(model_profiles.model_profiles_path(fixture.agent.home_paths))
            provider_id = data["profiles"][fixture.candidate]["provider_id"]
            provider = {**data["providers"][provider_id], "api_key": "rotated-test-key"}
            model_profiles.execute_model_profile_operation(fixture.agent, "save_provider", {"provider_id": provider_id, "provider": provider, "editing": True})
        elif change == "compact":
            fixture.agent.conversation_store.threads.update_atomic(fixture.thread_id, lambda row: replace(row, compact_generation=row.compact_generation + 1))
        elif change == "deadline":
            adoption.outcome = replace(adoption.outcome, deadline=0)
        elif change == "payload":
            state.messages.append({"role": "user", "content": "迟到消息"})
        elif change == "runtime":
            update_json_file_atomic(fixture.context.request_path, lambda row: {**row, "runtime_authority": {**row["runtime_authority"], "attempt_id": "other-attempt"}})

    checked = before_final_check(monkeypatch, mutate)
    request_execution._run_gateway_ask(fixture.context)
    assert len(checked) == 1 and len(decision.calls) == 1
    assert [row[0]["model"] for row in business] == ["original-model"]
    assert "迟到消息" not in json.dumps(business[0][0], ensure_ascii=False)
    current = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert current.model_profile_id == fixture.original
    assert current.model_selection_revision == before.model_selection_revision + (change == "same_explicit")
    assert model_adoption.MODEL_ADOPTION_KEY not in current.metadata
    main = model_call_summary(fixture.agent, request_id=fixture.context.request_id)["purpose_breakdown"]["main"]
    assert main["physical_model_attempt_count"] == 2
    assert main["provider_http_attempt_count"] == 1
    assert fixture.context.request[MODEL_OBSERVATION_KEY]["status"] == "retained"
    assert _HOST.get() is None


def test_too_small_actual_payload_preserves_original_request(tmp_path, monkeypatch):
    fixture = actual_request(tmp_path, window=4096)
    decision = install_backend(monkeypatch, fixture)
    business, _ = fake_http(monkeypatch, fixture)
    request_execution._run_gateway_ask(fixture.context)
    assert [row[0]["model"] for row in business] == ["original-model"]
    assert len(decision.calls) == 1
    assert fixture.context.request[MODEL_OBSERVATION_KEY]["status"] == "retained"
    main = model_call_summary(fixture.agent, request_id=fixture.context.request_id)["purpose_breakdown"]["main"]
    assert main["physical_model_attempt_count"] == 1


@pytest.mark.parametrize("block", [
    {"type": "thinking", "thinking": "内部推理", "signature": "opaque-signature"},
    {"type": "redacted_thinking", "data": "opaque-data"},
    {"type": "image", "source": {"type": "url", "url": "https://example.invalid/image.png"}},
    {"type": "future_provider_block", "data": "unknown"},
])
def test_raw_history_rejects_unknown_before_adapter_can_discard(block):
    from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestInput
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn

    assert not model_adoption._portable_history(ToolLoopRequestInput(tool_ir_history=(AssistantTurn(content_blocks=[block]),)))
    assert not model_adoption._portable_history(ToolLoopRequestInput(provider_history_messages=({"role": "assistant", "content": [block]},)))


def test_http_failure_never_sends_original_model_or_redraws_decision(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError

    fixture = actual_request(tmp_path)
    decision = install_backend(monkeypatch, fixture)

    def fail(_wire):
        raise ProviderRequestRejectedError("test rejection", status_code=400)

    business, _ = fake_http(monkeypatch, fixture, on_business=fail)
    with pytest.raises(ProviderRequestRejectedError):
        request_execution._run_gateway_ask(fixture.context)
    assert [row[0]["model"] for row in business] == ["candidate-large"]
    assert len(decision.calls) == 1 and _HOST.get() is None
    current = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert current.model_profile_id == fixture.candidate
    assert current.metadata[model_adoption.MODEL_ADOPTION_KEY]["status"] == "send_intent_uncertain"


@pytest.mark.parametrize("error", [InterruptedError, KeyboardInterrupt])
def test_user_stop_before_candidate_send_does_not_fallback(tmp_path, monkeypatch, error):
    fixture = actual_request(tmp_path)
    install_backend(monkeypatch, fixture)
    business, _ = fake_http(monkeypatch, fixture)

    def cancel(_adoption, _state):
        raise error("stopped")

    before_final_check(monkeypatch, cancel)
    with pytest.raises(error):
        request_execution._run_gateway_ask(fixture.context)
    assert business == [] and _HOST.get() is None
    assert fixture.agent.conversation_store.threads.require(fixture.thread_id).model_profile_id == fixture.original


def test_persisted_send_intent_survives_reopen_and_never_reselects(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_model_observation import GatewayModelObservation
    from agent_py_agent.agent.gateway_parts.io import read_json_file

    fixture = actual_request(tmp_path)
    decision = install_backend(monkeypatch, fixture)
    fake_http(monkeypatch, fixture)
    request_execution._run_gateway_ask(fixture.context)
    reopened = ConversationStore(fixture.agent.conversation_store.storage.root)
    current = reopened.threads.require(fixture.thread_id)
    assert current.model_profile_id == fixture.candidate
    assert current.metadata[model_adoption.MODEL_ADOPTION_KEY]["status"] == "send_intent_uncertain"
    context = replace(fixture.context, request=read_json_file(fixture.context.request_path))
    with model_profiles.capture_selected_model_read() as captures:
        model_profiles.selected_model_config(fixture.agent, profile_id=fixture.candidate)
    observer = GatewayModelObservation(context, captures[0], reopened.claims.load(fixture.thread_id))
    observer(current)
    assert len(decision.calls) == 1 and observer.adoption is None


@pytest.mark.parametrize("unit,repeats", [("中文材料", 12_000), ("large-schema-", 12_000), ("history: ", 20_000)], ids=["chinese", "schema", "history"])
def test_conservative_budget_rejects_utf8_even_when_old_estimator_underreads(monkeypatch, unit, repeats):
    from types import SimpleNamespace

    material = unit * repeats
    payload = {"messages": [{"role": "user", "content": material}], "tools": [{"name": "large", "input_schema": {"description": material}}], "max_tokens": 4096}
    monkeypatch.setattr(model_adoption, "estimate_tokens", lambda _value: 1)
    with pytest.raises(ValueError, match="request_capacity_exceeded"):
        model_adoption._capacity(SimpleNamespace(model_context_window_explicit=True, model_context_window_tokens=250_000), payload)
    validation = model_adoption._capacity(SimpleNamespace(model_context_window_explicit=True, model_context_window_tokens=1_000_000), payload)
    assert validation["estimated"] is True
    assert validation["conservative_input_bound"] > len(json.dumps(payload, ensure_ascii=False).encode())


@pytest.mark.parametrize("phase", ["before_replace", "after_replace", "readback_unknown"])
def test_atomic_write_failure_does_not_guess_submission_or_duplicate_http(tmp_path, monkeypatch, phase):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import io as json_io

    fixture = actual_request(tmp_path)
    install_backend(monkeypatch, fixture)
    business, _ = fake_http(monkeypatch, fixture)
    thread_path = fixture.agent.conversation_store.storage.thread_path(fixture.thread_id)
    original_write = json_io._replace_with_retry
    original_require = fixture.agent.conversation_store.threads.require
    writes = []

    def fail_write(tmp, path):
        if path == thread_path and model_adoption.MODEL_ADOPTION_KEY in tmp.read_text(encoding="utf-8") and not writes:
            writes.append(path)
            if phase != "before_replace":
                original_write(tmp, path)
            raise OSError("injected atomic persistence failure")
        return original_write(tmp, path)

    def require(thread_id):
        host = _HOST.get()
        if phase == "readback_unknown" and writes and host and host.adoption.commit_uncertain:
            raise OSError("injected unreadable commit")
        return original_require(thread_id)

    monkeypatch.setattr(json_io, "_replace_with_retry", fail_write)
    monkeypatch.setattr(fixture.agent.conversation_store.threads, "require", require)
    if phase == "before_replace":
        request_execution._run_gateway_ask(fixture.context)
        assert [row[0]["model"] for row in business] == ["original-model"]
    else:
        with pytest.raises(OSError):
            request_execution._run_gateway_ask(fixture.context)
        assert business == []
    assert len(writes) == 1
    thread = ConversationStore(fixture.agent.conversation_store.storage.root).threads.require(fixture.thread_id)
    assert thread.model_profile_id == (fixture.original if phase == "before_replace" else fixture.candidate)
    assert _HOST.get() is None


def test_text_tool_history_allows_actual_list_and_frozen_tuple():
    from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestInput
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn

    blocks = [{"type": "text", "text": "普通中文"}, {"type": "tool_use", "id": "t", "name": "read_file", "input": {"path": "材料"}}]
    for content in (blocks, tuple(blocks)):
        assert model_adoption._portable_history(ToolLoopRequestInput(tool_ir_history=(AssistantTurn(content_blocks=content),)))


def test_noncooperative_probe_timeout_retains_without_late_adoption(tmp_path, monkeypatch):
    import threading

    fixture = actual_request(tmp_path, tools=True)
    patch(fixture.agent, {"timeout_seconds": 1, "stage_timeout_seconds": 1})
    decision = install_backend(monkeypatch, fixture)
    entered, released, exited = threading.Event(), threading.Event(), threading.Event()

    def wait_probe():
        entered.set()
        try:
            assert released.wait(5)
        finally:
            exited.set()

    business, _ = fake_http(monkeypatch, fixture, before_probe=wait_probe)
    try:
        request_execution._run_gateway_ask(fixture.context)
        assert entered.is_set()
        assert [row[0]["model"] for row in business] == ["original-model"]
        assert len(decision.calls) == 1
    finally:
        released.set()
        assert exited.wait(2)
    current = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert current.model_profile_id == fixture.original
    assert model_adoption.MODEL_ADOPTION_KEY not in current.metadata


def test_actual_provider_overflow_stays_on_adopted_model(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderContextWindowError

    fixture = actual_request(tmp_path)
    decision = install_backend(monkeypatch, fixture)
    attempted = []

    def overflow_once(wire):
        attempted.append(wire["model"])
        if len(attempted) == 1:
            raise ProviderContextWindowError("typed test overflow")

    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError

    business, _ = fake_http(monkeypatch, fixture, on_business=overflow_once)
    compact_models = []
    original_compact = request_execution._gateway_compact_overflowing_turn

    def compact(context, *args, **kwargs):
        compact_models.append(context.agent.config.model_name)
        return original_compact(context, *args, **kwargs)

    monkeypatch.setattr(request_execution, "_gateway_compact_overflowing_turn", compact)
    before = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    with pytest.raises(ConversationCompactError) as raised:
        request_execution._run_gateway_ask(fixture.context)
    assert raised.value.code == "COMPACT_SOURCE_EMPTY"
    after = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert (after.compact_generation, after.compact_checkpoint_id) == (before.compact_generation, before.compact_checkpoint_id)
    assert compact_models == ["candidate-large"]
    assert len(business) == 1 and business[0][0]["model"] == "candidate-large"
    assert len(decision.calls) == 1 and _HOST.get() is None


def test_catalog_rotation_after_decision_prevents_candidate_probe(tmp_path, monkeypatch):
    fixture = actual_request(tmp_path, tools=True)
    install_backend(monkeypatch, fixture)
    business, probes = fake_http(monkeypatch, fixture)
    original_select = model_adoption.GatewayModelAdoption.select
    changed = []

    def select(adoption, agent, params, prompt):
        if not changed:
            data = model_profiles.read_model_profiles(model_profiles.model_profiles_path(agent.home_paths))
            provider_id = data["profiles"][fixture.candidate]["provider_id"]
            model_profiles.execute_model_profile_operation(agent, "save_provider", {
                "provider_id": provider_id, "provider": {**data["providers"][provider_id], "api_key": "new-test-key"}, "editing": True,
            })
            changed.append(True)
        return original_select(adoption, agent, params, prompt)

    monkeypatch.setattr(model_adoption.GatewayModelAdoption, "select", select)
    request_execution._run_gateway_ask(fixture.context)
    assert [row["model"] for row in probes] == ["original-model"]
    assert [row[0]["model"] for row in business] == ["original-model"]


def test_stop_while_recording_retention_does_not_send_original(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayModelObservationWriter

    fixture = actual_request(tmp_path, window=4096)
    install_backend(monkeypatch, fixture)
    business, _ = fake_http(monkeypatch, fixture)

    def stop(_writer, _result):
        raise InterruptedError("closed turn")

    monkeypatch.setattr(GatewayModelObservationWriter, "record_adoption", stop)
    with pytest.raises(InterruptedError):
        request_execution._run_gateway_ask(fixture.context)
    assert business == []


@pytest.mark.parametrize("tools", [False, True])
def test_known_chat_protocol_uses_actual_provider_projection(tmp_path, monkeypatch, tools):
    fixture = actual_request(tmp_path, tools=tools, candidate_backend="openai_compatible")
    install_backend(monkeypatch, fixture)
    business, probes = fake_http(monkeypatch, fixture)
    request_execution._run_gateway_ask(fixture.context)
    assert [row[0]["model"] for row in business] == ["candidate-large"]
    assert "system" not in business[0][0]
    assert bool(probes) == tools
