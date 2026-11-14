"""压缩预算必须贯穿真实适配器；只替换 HTTP，不调用真实模型。"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import compact_summary_output_reserve_tokens
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.conversation.auxiliary_model_call import AuxiliaryModelCallRequest
from agent_py_agent.agent.conversation.compact_request_budget import (
    generate_bounded_compact_response,
)
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.settings.config import AgentConfig


def _responses_agent(monkeypatch, *, normal=128000, window=272000, mode="auto", **changes):
    values = dict(model_backend="openai_responses", model_name="gpt-6-test",
                  api_base="https://fixture.invalid/v1", api_key="test-key", stream_enabled=False,
                  max_tokens=normal, model_context_window_tokens=window,
                  model_reasoning_control="effort", model_reasoning_effort="max",
                  model_reasoning_levels=["low", "medium", "high", "max"], model_reasoning_update_items=mode)
    config = replace(AgentConfig(), **(values | changes))
    backend = get_backend(config.model_backend, config)
    payloads = []

    def send(request):
        payloads.append(deepcopy(request.payload))
        return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "已核实的事实"}]}]}

    monkeypatch.setattr(http, "post_json", send)
    return SimpleNamespace(backend=backend, config=config), payloads


def _summary_request(agent, **changes):
    return AuxiliaryModelCallRequest(agent=agent,
        prompt=CacheStructuredPrompt("稳定的宿主前缀", "只总结已完成的事实"),
        messages=[{"role": "user", "content": "原始问题"},
                  {"role": "assistant", "content": "原始结果"}],
        system_instruction="保留来源", purpose="conversation_compact_summary",
        thread_id="budget-test", **changes)


@pytest.mark.parametrize("normal", [32768, 65536, 128000])
def test_disabled_tail_updates_preserve_thread_partition_and_legacy_reserve(monkeypatch, normal):
    agent, payloads = _responses_agent(monkeypatch, normal=normal, mode="off")
    request = _summary_request(agent)
    history = deepcopy(request.messages)
    legacy = agent.backend.max_tokens
    assert compact_summary_output_reserve_tokens(agent) == legacy
    result = generate_bounded_compact_response(request)
    assert result.text == "已核实的事实"
    assert len(payloads) == 1
    assert payloads[0]["max_output_tokens"] == legacy
    assert payloads[0]["reasoning"] == {"effort": "max"}
    assert not any(item.get("type") == "configuration_update" for item in payloads[0]["input"])
    assert [item for item in payloads[0]["input"] if item.get("role") in {"user", "assistant"}][:2] == history
    assert request.messages == history
    assert agent.backend.max_tokens == legacy


def test_parameter_registry_exposes_compact_controls_and_derived_reserve(monkeypatch):
    from agent_py_agent.agent.settings.parameter_registry import (
        applied_value,
        applied_value_with,
        parameter_registry,
    )

    agent, _ = _responses_agent(monkeypatch)
    registry = parameter_registry()
    keys = ("memory_compact_summary_max_output_tokens", "memory_compact_reasoning_level",
            "memory_compact_trigger_percent", "memory_compact_auto_trigger_max_tokens", "model_reasoning_update_items")
    for key in keys:
        spec = registry[key]
        assert spec.description and any("\u4e00" <= char <= "\u9fff" for char in spec.description)
        assert spec.effect
        if key == "model_reasoning_update_items":
            assert spec.source == "model_profile" and not spec.writable and not spec.user_editable
            assert not spec.writer and "/model" in spec.description
        else:
            assert spec.writable and spec.user_editable
    applied = applied_value(keys[0], agent.config)
    assert applied is not None
    assert applied[0]["basis"] == "configuration_preview"
    assert applied[0]["preview_tokens"] == compact_summary_output_reserve_tokens(agent) == 16384
    assert applied[0]["runtime_effective_tokens"] is None
    assert applied_value_with(keys[0], "0", agent.config)[0]["preview_tokens"] == 128000
    assert applied_value_with(keys[0], "65536", agent.config)[0]["preview_tokens"] == 65536
    assert applied_value_with(keys[0], "bad", agent.config) is None
    assert applied_value("max_tokens", agent.config)[0] == 68000


@pytest.mark.parametrize("brief", [False, True])
def test_compact_parameter_preview_does_not_claim_runtime_provider_metadata(monkeypatch, brief):
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    from agent_py_agent.agent.tooling.user_config_tool import _spec_view

    agent, sent = _responses_agent(monkeypatch)
    agent.backend.model_metadata = {"max_output_tokens": 8192}
    metadata = deepcopy(agent.backend.model_metadata)
    assert compact_summary_output_reserve_tokens(agent) == 8192
    key = "memory_compact_summary_max_output_tokens"
    view = _spec_view(parameter_registry()[key], agent.config, brief=brief)
    preview = view["applied_value"]
    assert isinstance(preview, dict), "配置入口不能把无 metadata 的预览伪装成实际运行值"
    assert preview["basis"] == "configuration_preview"
    assert preview["preview_tokens"] == 16384
    assert preview["runtime_effective_tokens"] is None
    assert "未知" in preview["note"] and "供应商" in preview["note"]
    assert view["running_value"] == "16384"
    assert agent.backend.model_metadata == metadata and sent == []


def test_compact_cap_is_shared_by_admission_partition_options_and_wire(monkeypatch):
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.conversation import compact_request_budget as budgets
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
    from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection

    agent, sent = _responses_agent(monkeypatch)
    request = _summary_request(agent)
    assert budgets.compact_cache_surface_budget(agent) == 253568
    assert budgets.compact_summary_budget(agent) == 199168
    parts = [(["earlier"], ()), (["later"], ())]
    partition_request = SimpleNamespace(agent=agent, interrupt_check=None, tool_source=None, calibration=None,
        thread=SimpleNamespace(thread_id="t", compact_generation=0), current_prompt="继续",
        policy=SimpleNamespace(trigger_tokens=217600, recovery_target_tokens=70000),
        request_projector=lambda view: ConversationCompactProjection(50000, object()))
    selected = compact._select_compact_partition(partition_request, parts, ())
    assert selected == parts[0] and selected[0] is parts[0][0]
    source = CompactMessageSource(lambda: iter(request.messages), preserve_prefix=True)
    observed = []
    original = budgets.generate_auxiliary_model_response

    def call(admitted):
        observed.append(admitted)
        return original(admitted)

    monkeypatch.setattr(budgets, "generate_auxiliary_model_response", call)
    result = budgets.generate_bounded_compact_response(replace(request, messages=None), message_source=source)
    assert result.text and len(observed) == len(sent) == 1
    assert observed[0].token_budget.output_tokens == sent[0]["max_output_tokens"] == 16384
    assert result.request_token_budget is observed[0].token_budget
    assert observed[0].messages == request.messages
    assert sent[0]["reasoning"] == {"effort": "max"}
    assert sent[0]["input"][-2] == {"type": "configuration_update", "reasoning": {"effort": "low"}}


@pytest.mark.parametrize("normal", [32768, 65536, 128000])
def test_large_normal_ceiling_does_not_inflate_controlled_compact_reserve(monkeypatch, normal):
    from agent_py_agent.agent.conversation.compact_request_budget import (
        compact_cache_surface_budget,
    )

    agent, sent = _responses_agent(monkeypatch, normal=normal)
    before = (agent.backend.max_tokens, agent.backend.configured_output_ceiling)
    result = generate_bounded_compact_response(_summary_request(agent))
    assert compact_summary_output_reserve_tokens(agent) == sent[0]["max_output_tokens"] == 16384
    assert compact_cache_surface_budget(agent) == 253568
    assert result.request_token_budget.output_tokens == 16384
    assert (agent.backend.max_tokens, agent.backend.configured_output_ceiling) == before


@pytest.mark.parametrize("control,levels,target,reason", [
    ("none", ["low", "high"], "low", "downshift_unavailable"),
    ("effort", ["medium", "high"], "low", "downshift_unavailable"),
    ("budget", ["low", "high"], "low", "downshift_unavailable"),
    ("effort", ["low", "high"], "", "downshift_disabled"),
    ("effort", ["low", "high"], "auto", "downshift_disabled"),
    ("effort", ["low", "high"], "off", "downshift_disabled"),
])
def test_unrepresentable_reasoning_uses_explicit_legacy_reserve(monkeypatch, control, levels, target, reason):
    agent, sent = _responses_agent(monkeypatch, model_reasoning_control=control,
        model_reasoning_levels=levels, memory_compact_reasoning_level=target)
    result = generate_bounded_compact_response(_summary_request(agent))
    assert sent[0]["max_output_tokens"] == result.request_token_budget.output_tokens == 68000
    assert agent._model_call_ledger.records()[-1].metadata["compact_downshift_reason"] == reason
    assert not any(item.get("type") == "configuration_update" for item in sent[0]["input"])


@pytest.mark.parametrize("normal", [32768, 65536, 128000])
def test_zero_summary_cap_inherits_normal_ceiling(monkeypatch, normal):
    agent, sent = _responses_agent(monkeypatch, normal=normal, memory_compact_summary_max_output_tokens=0)
    result = generate_bounded_compact_response(_summary_request(agent))
    assert compact_summary_output_reserve_tokens(agent) == normal
    assert sent[0]["max_output_tokens"] == result.request_token_budget.output_tokens == normal
    assert agent.backend.max_tokens == min(normal, 68000)


@pytest.mark.parametrize("reasoning_effort,expected_reasoning", [("max", {"effort": "max"}), ("off", None)])
def test_noncompact_auxiliary_is_unchanged(monkeypatch, reasoning_effort, expected_reasoning):
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        generate_auxiliary_model_response,
    )

    agent, sent = _responses_agent(monkeypatch, model_reasoning_effort=reasoning_effort)
    request = replace(_summary_request(agent), purpose="auxiliary")
    before = deepcopy(request.messages)
    result = generate_auxiliary_model_response(request)
    assert sent[0]["max_output_tokens"] == 68000
    # 普通辅助调用沿线程真实档位；不能套用 Compact 的 low 降档或尾部更新。
    assert sent[0].get("reasoning") == expected_reasoning
    assert not any(item.get("type") == "configuration_update" for item in sent[0]["input"])
    assert request.messages == before and request.token_budget is None
    assert result.request_token_budget is None


def test_legacy_backend_cannot_silently_drop_required_cap():
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        generate_auxiliary_model_response,
    )

    calls = []

    class Legacy:
        name = "openai_responses"
        max_tokens = 65536
        supports_provider_request_options = True
        reasoning_control = "effort"
        reasoning_levels = ("low", "high")

        def generate(self, prompt):
            calls.append(prompt)
            return ModelResponse(text="旧后端摘要", backend="legacy")

    agent = SimpleNamespace(backend=Legacy(), config=AgentConfig())
    request = AuxiliaryModelCallRequest(agent=agent, prompt="短原文", purpose="conversation_compact_summary")
    with pytest.raises(TypeError, match="request_options"):
        generate_auxiliary_model_response(request)
    assert not calls
    agent.backend.supports_provider_request_options = False
    result = generate_auxiliary_model_response(request)
    assert result.text == "旧后端摘要" and calls == ["短原文"]
    assert result.request_token_budget.output_tokens == 65536


def test_valid_prefix_does_not_require_a_feasible_segment_geometry(monkeypatch):
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource

    agent, sent = _responses_agent(monkeypatch, window=40000, memory_compact_summary_max_output_tokens=32768)
    request = _summary_request(agent)
    original = deepcopy(request.messages)
    source = CompactMessageSource(lambda: iter(request.messages), preserve_prefix=True)
    response = generate_bounded_compact_response(replace(request, messages=None), message_source=source)
    assert response.text and len(sent) == 1
    assert sent[0]["max_output_tokens"] == 32768
    assert request.messages == original


def test_strict_truncation_preserves_source_and_checkpoint(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
    from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
    from agent_py_agent.tests.test_compact_capacity_facts import _transcript_case

    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=4)
    controlled, _ = _responses_agent(monkeypatch)
    agent.backend = controlled.backend
    agent.config = replace(controlled.config, my_agent_home=agent.config.my_agent_home)
    source_path = store.storage.message_path(thread.thread_id)
    source_bytes = source_path.read_bytes()
    checkpoint = store.threads.load(thread.thread_id).compact_generation
    calls = []

    def truncated(request):
        calls.append(deepcopy(request.payload))
        return {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "不完整摘要"}]}]}

    monkeypatch.setattr(http, "post_json", truncated)
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(force=True,
            request_projector=lambda view: ConversationCompactProjection(50000 if view.is_candidate else 100000, object())))
    assert error.value.code == "COMPACT_SUMMARY_TRUNCATED" and len(calls) == 2
    assert source_path.read_bytes() == source_bytes
    assert store.threads.load(thread.thread_id).compact_generation == checkpoint == 0
    assert not store.threads.load(thread.thread_id).summary


def test_subscription_reserve_is_not_reported_as_wire_cap(monkeypatch):
    agent, sent = _responses_agent(monkeypatch)
    agent.backend.auth_ref = {"mode": "chatgpt"}
    result = generate_bounded_compact_response(_summary_request(agent))
    assert "max_output_tokens" not in sent[0]
    assert result.request_token_budget.output_tokens == 16384
    assert result.request_token_budget.server_managed_output and not result.request_token_budget.shared_window
    record = agent._model_call_ledger.records()[-1]
    assert record.output_tokens_estimate == 16384 and record.metadata["server_managed_output"]


def test_remote_compaction_contract_is_preserved(monkeypatch):
    from agent_py_agent.agent.conversation.compact_remote import (
        RemoteCompactionRequest,
        remote_compaction_enabled,
        request_remote_compaction,
    )
    from agent_py_agent.tests.test_request_token_budget import _main_response

    agent, _ = _responses_agent(monkeypatch, request_timeout=0)
    assert not remote_compaction_enabled(agent), "普通API不能仅凭模型名启用远端压缩"
    agent.backend.auth_ref = {"mode": "chatgpt"}
    assert remote_compaction_enabled(agent)
    sent = []

    def send(request):
        sent.append(deepcopy(request.payload))
        return {"status": "completed", "output": [{"type": "compaction", "encrypted_content": "opaque-fixture"}]}

    monkeypatch.setattr(http, "post_json", send)
    history = [{"role": "user", "content": "完整原始历史"}]
    before = deepcopy(history)
    result = request_remote_compaction(RemoteCompactionRequest(agent,
        CacheStructuredPrompt("冻结前缀", ""), history, thread_id="remote-budget"))
    assert result["item"] == {"type": "compaction", "encrypted_content": "opaque-fixture"}
    assert sent[0]["input"][-1] == {"type": "compaction_trigger"}
    assert sent[0]["input"][-2]["type"] == "configuration_update"
    assert sent[0]["input"][-2]["reasoning"] == {"effort": "low"}
    assert "max_output_tokens" not in sent[0] and "truncation" not in sent[0]
    assert not any(item.get("content") == "只总结已完成的事实" for item in sent[0]["input"])
    assert history == before
    _main_response(monkeypatch, agent, 50000)
    assert sent[-1]["reasoning"] == {"effort": "max"}
    assert not any(item.get("type") in {"configuration_update", "compaction_trigger"} for item in sent[-1]["input"])


def test_summary_usage_uses_resolved_cap_without_extending_fixed_timeout(monkeypatch):
    from agent_py_agent.agent.conversation import auxiliary_model_call as auxiliary
    from agent_py_agent.agent.conversation import compact_request_budget as budgets

    agent, _ = _responses_agent(monkeypatch, request_timeout=20, estimated_output_tokens_per_second=20)
    admitted, sent = [], []
    original = budgets.generate_auxiliary_model_response

    def observe(request):
        admitted.append(request)
        return original(request)

    def send(path, payload, headers, **options):
        sent.append((deepcopy(payload), options))
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "事实摘要"}]}]}

    monkeypatch.setattr(budgets, "generate_auxiliary_model_response", observe)
    monkeypatch.setattr(agent.backend, "request_json", send)
    result = generate_bounded_compact_response(_summary_request(agent))
    budget = admitted[0].token_budget
    record = agent._model_call_ledger.records()[-1]
    expected = auxiliary._auxiliary_timeout_plan(admitted[0], budget.input_tokens).total_deadline_seconds
    assert record.input_tokens == budget.input_tokens
    assert record.output_tokens_estimate == budget.output_tokens == sent[0][0]["max_output_tokens"] == 16384
    assert sent[0][1]["total_deadline_seconds"] == expected
    assert expected == auxiliary.AUXILIARY_CALL_FLOOR_SECONDS == 240
    assert result.request_token_budget is budget


def test_calibrated_admission_budget_reaches_auxiliary_dispatch_unchanged(monkeypatch):
    from agent_py_agent.agent.conversation import auxiliary_model_call as auxiliary
    from agent_py_agent.agent.conversation import compact_request_budget as budgets
    from agent_py_agent.agent.conversation.compact_calibration import (
        CompactRequestCalibration,
        calibrated_compact_request_tokens,
    )

    agent, sent = _responses_agent(monkeypatch)
    request = replace(_summary_request(agent), messages=[{"role": "user", "content": "x" * 800000}])
    raw = budgets.compact_request_tokens(request)
    assert raw > budgets.compact_cache_surface_budget(agent)
    calibration = CompactRequestCalibration(raw + 10000, (raw + 10000) // 2)
    count = calibrated_compact_request_tokens(raw, calibration)
    monkeypatch.setattr(auxiliary, "_auxiliary_input_tokens", lambda request: pytest.fail("不能用较大raw重建已准入预算"))
    result = generate_bounded_compact_response(request, calibration=calibration)
    assert len(sent) == 1 and sent[0]["input"][1:-2] == request.messages
    assert sent[0]["input"][0] == {"role": "system", "content": "保留来源\n\n稳定的宿主前缀"}
    assert result.request_token_budget.input_tokens == count
    assert agent._model_call_ledger.records()[-1].input_tokens == count


def test_structured_compact_forwards_low_intent_and_frozen_budget(monkeypatch):
    agent, sent = _responses_agent(monkeypatch)
    schema = {"type": "object", "properties": {"fact": {"type": "boolean"}}, "required": ["fact"], "additionalProperties": False}
    observed = []
    original = agent.backend._generate

    def generate(request):
        observed.append(request)
        return original(request)

    monkeypatch.setattr(agent.backend, "_generate", generate)
    result = generate_bounded_compact_response(_summary_request(agent, response_schema=schema))
    assert observed[0].reasoning_update_effort == "low"
    assert observed[0].token_budget is result.request_token_budget
    assert sent[0]["max_output_tokens"] == result.request_token_budget.output_tokens == 16384
    assert sent[0]["reasoning"] == {"effort": "max"}
    assert sent[0]["input"][-2] == {"type": "configuration_update", "reasoning": {"effort": "low"}}
    assert sent[0]["input"][-1]["content"] == "只总结已完成的事实"
    assert sent[0]["text"]["format"]["schema"] == schema


@pytest.mark.parametrize("calibrated", [False, True])
def test_anthropic_structured_surface_and_repair_have_fresh_host_budgets(monkeypatch, calibrated):
    from agent_py_agent.agent.conversation import compact_request_budget as budgets
    from agent_py_agent.agent.conversation.compact_calibration import (
        CompactRequestCalibration,
        calibrated_compact_request_tokens,
    )

    agent, _ = _responses_agent(monkeypatch, model_backend="anthropic_compatible", model_reasoning_control="budget")
    schema = {"type": "object", "properties": {"fact": {"type": "boolean"}}, "required": ["fact"]}
    requests, payloads = [], []
    original = agent.backend._generate_request
    calibration = CompactRequestCalibration(10000, 5000) if calibrated else None

    def generate(request):
        requests.append(request)
        return original(request)

    def send(call):
        payloads.append(deepcopy(call.payload))
        if len(payloads) == 1:
            return {"content": [{"type": "text", "text": "没有遵守schema"}], "stop_reason": "end_turn"}
        return {"content": [{"type": "tool_use", "id": "schema-result", "name": "my_agent_structured_output", "input": {"fact": True}}], "stop_reason": "tool_use"}

    monkeypatch.setattr(agent.backend, "_generate_request", generate)
    monkeypatch.setattr(http, "post_json", send)
    response = generate_bounded_compact_response(_summary_request(agent, response_schema=schema), calibration=calibration)
    assert response.text == '{"fact": true}' and len(requests) == len(payloads) == 2
    for item, payload in zip(requests, payloads):
        surface = AuxiliaryModelCallRequest(agent, item.prompt, messages=item.messages, tools=item.tools,
            system_instruction=item.system_instruction, purpose="conversation_compact_summary", thread_id="budget-test")
        measured = calibrated_compact_request_tokens(budgets.compact_request_tokens(surface), calibration)
        assert item.token_budget.input_tokens == measured
        assert item.token_budget.output_tokens == payload["max_tokens"] == agent.backend.max_tokens == 68000
    assert requests[1].token_budget is not requests[0].token_budget
    assert requests[1].token_budget.input_tokens > requests[0].token_budget.input_tokens
    assert response.request_token_budget is requests[1].token_budget
    assert agent._model_call_ledger.records()[-1].input_tokens == requests[0].token_budget.input_tokens


def test_structured_repair_capacity_failure_stops_before_second_io(monkeypatch):
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
    from agent_py_agent.agent.conversation.compact_request_budget import (
        compact_cache_surface_budget,
    )
    from agent_py_agent.tests.test_compact_budget_watermark import _request_at_tokens

    agent, _ = _responses_agent(monkeypatch, window=64000, model_backend="anthropic_compatible", model_reasoning_control="budget")
    request = _summary_request(agent, response_schema={"type": "object"})
    request = _request_at_tokens(request, compact_cache_surface_budget(agent) - 1)
    source = CompactMessageSource(lambda: iter(request.messages), preserve_prefix=True)
    before = deepcopy(request.messages)
    sent = []

    def send(call):
        sent.append(deepcopy(call.payload))
        return {"content": [{"type": "text", "text": "缺少schema信封"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(http, "post_json", send)
    with pytest.raises(ConversationCompactError) as error:
        generate_bounded_compact_response(replace(request, messages=None), message_source=source)
    assert error.value.code == "COMPACT_PREFIX_TOO_LARGE" and len(sent) == 1
    assert request.messages == before and sent[0]["max_tokens"] == agent.backend.max_tokens == 16000


def test_segment_and_repair_keep_calibrated_budgets_and_complete_source(monkeypatch):
    import json

    from agent_py_agent.agent.conversation import compact_request_budget as budgets
    from agent_py_agent.agent.conversation.compact_calibration import (
        CompactRequestCalibration,
        calibrated_compact_request_tokens,
    )
    from agent_py_agent.tests.test_compact_request_budget import segment_part

    agent, _ = _responses_agent(monkeypatch, window=64000)
    request = replace(_summary_request(agent), messages=[{"role": "user", "content": "x" * 400000}])
    calibration = CompactRequestCalibration(1000000, 500000)
    observed, payloads = [], []
    original = budgets.generate_auxiliary_model_response

    def observe(admitted):
        observed.append(admitted)
        return original(admitted)

    def send(call):
        payloads.append(deepcopy(call.payload))
        text = "" if len(payloads) % 2 else "保留此前与本段事实"
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    monkeypatch.setattr(budgets, "generate_auxiliary_model_response", observe)
    monkeypatch.setattr(http, "post_json", send)
    response = generate_bounded_compact_response(request, calibration=calibration)
    assert response.text and len(observed) == len(payloads) and len(observed) >= 4
    for item in observed:
        expected = calibrated_compact_request_tokens(budgets.compact_request_tokens(item), calibration)
        assert item.token_budget.input_tokens == expected
        assert item.token_budget.output_tokens == 16384
    for index in range(0, len(observed), 2):
        assert observed[index].token_budget is not observed[index + 1].token_budget
    assert "".join(segment_part(item.prompt)[3] for item in observed[::2]) == json.dumps(request.messages, ensure_ascii=False)
