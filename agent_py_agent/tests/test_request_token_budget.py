"""实测预算与旧直接调用分别验证，不用模型名字推断物理容量。"""
from dataclasses import FrozenInstanceError

import pytest

from agent_py_agent.agent.backends.request_budget import (
    RequestBudgetExceeded,
    resolve_request_token_budget,
)
from agent_py_agent.tests.test_compact_output_policy import _responses_agent


def test_exact_fit_and_no_capacity_never_fabricate_one_token():
    budget = resolve_request_token_budget(input_tokens=4095, context_window_tokens=6144,
                                         output_ceiling=128000, shared_window=True)
    assert budget.output_tokens == 1
    with pytest.raises(FrozenInstanceError):
        budget.output_tokens = 2
    for count in (4096, 6144, 100000):
        with pytest.raises(RequestBudgetExceeded) as caught:
            resolve_request_token_budget(input_tokens=count, context_window_tokens=6144,
                                         output_ceiling=128000, shared_window=True)
        assert caught.value.reason == "input_overflow"
        assert caught.value.input_tokens == count
        assert caught.value.error_code == "REQUEST_OUTPUT_BUDGET_UNAVAILABLE"


@pytest.mark.parametrize("window", [32000, 64000])
def test_small_window_is_valid_or_explicitly_rejected(window):
    budget = resolve_request_token_budget(input_tokens=window-16384-2048,
        context_window_tokens=window, output_ceiling=16384, shared_window=True,
        require_full_output=True)
    assert budget.output_tokens == 16384
    with pytest.raises(RequestBudgetExceeded) as caught:
        resolve_request_token_budget(input_tokens=0, context_window_tokens=8000,
            output_ceiling=16384, shared_window=True, require_full_output=True)
    assert caught.value.reason == "invalid_geometry"


def test_main_cap_tracks_100k_200k_50k_inputs(monkeypatch):
    agent, sent = _responses_agent(monkeypatch, request_timeout=0)
    backend = agent.backend
    params = None
    for count, expected in [(100000, 128000), (200000, 69952), (50000, 128000)]:
        response, params, snapshots = _main_response(monkeypatch, agent, count, params=params)
        budget = response.request_token_budget
        assert sent[-1]["max_output_tokens"] == budget.output_tokens == expected
        assert budget.input_tokens == count and len(snapshots) == 1
        assert budget.input_tokens + budget.output_tokens + budget.safety_tokens <= 272000
        assert agent.backend is backend and backend.max_tokens == 68000
        assert backend.configured_output_ceiling == 128000
    assert len(sent) == 3


def _main_response(monkeypatch, agent, count, *, params=None, prompt="继续真实任务"):
    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.model.context_pressure import ModelVisibleContextSnapshot
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    if params is None:
        params = ToolLoopExecuteParams(user_prompt=prompt, memories=[], runtime_injections=[],
            prompt_files=[], tool_catalog_section="", tool_recommendations_section="", tool_context=[],
            effective_on_chunk=None, allowed_tools=None, write_boundary=None, task_attributes={},
            request_id="budget-main", run_id="budget-run", task_id="", one_shot_tool_calls=set(),
            executed_tools=[], archive_tool_calls=[], save=False,
            tool_protocol_snapshot=ToolProtocolSnapshot("budget-run", "native", ProviderToolCapability(
                provider="fixture", endpoint="local://fixture", model="fixture", stream=False,
                native_supported=True, evidence="fixture")))
    snapshots = []

    def snapshot(*args, **kwargs):
        value = ModelVisibleContextSnapshot(context_window_tokens=272000, compact_trigger_tokens=217600,
            current_tokens=count, prompt_tokens=count, messages_tokens=0, runtime_guidance_tokens=0,
            tool_schema_tokens=0, protocol="text", raw_estimated_tokens=count*2,
            context_surface_fingerprint="budget-calibrated", usage_binding=kwargs.get("usage_binding"))
        snapshots.append(value)
        return value

    monkeypatch.setattr(generation, "model_visible_context_snapshot", snapshot)
    response = generation.generate_model_response(generation.ModelGenerateParams(agent, params, prompt, 0))
    return response, params, snapshots


def test_measured_main_can_use_128k_without_mutating_legacy_default(monkeypatch):
    agent, sent = _responses_agent(monkeypatch, request_timeout=0)
    response, _params, snapshots = _main_response(monkeypatch, agent, 100000)
    assert sent[-1]["max_output_tokens"] == 128000
    assert response.request_token_budget.input_tokens == 100000
    assert response.request_token_budget.output_tokens == 128000
    assert len(snapshots) == 1
    assert agent.backend.max_tokens == 68000
    assert agent.backend.configured_output_ceiling == 128000


def test_provider_output_limit_reduces_nonshared_main_and_compact(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.model.call_runtime import request_budget_shared_window
    from agent_py_agent.agent.conversation.compact_request_budget import (
        generate_bounded_compact_response,
    )
    from agent_py_agent.tests.test_compact_output_policy import _summary_request

    agent, sent = _responses_agent(monkeypatch, window=0, request_timeout=0,
        memory_compact_summary_max_output_tokens=0)
    legacy = agent.backend.max_tokens
    agent.backend._provider_context_window_cache = 272000
    agent.backend.model_metadata = {"input_token_limit": 272000, "max_output_tokens": 8192}
    assert not request_budget_shared_window(agent)
    main, _, _ = _main_response(monkeypatch, agent, 1000)
    assert sent[-1]["max_output_tokens"] == main.request_token_budget.output_tokens == 8192
    assert not main.request_token_budget.shared_window
    for setting in [0, 16384]:
        agent.config = replace(agent.config, memory_compact_summary_max_output_tokens=setting,
                               model_reasoning_control="none")
        result = generate_bounded_compact_response(_summary_request(agent))
        assert sent[-1]["max_output_tokens"] == result.request_token_budget.output_tokens == 8192
    assert agent.backend.max_tokens == legacy


def test_legacy_and_probe_overrides_remain_lower_only(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.backends import BackendOptions, ProviderRequestOptions
    from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend

    agent, sent = _responses_agent(monkeypatch, window=128000)
    before = agent.backend.max_tokens
    for probe in [False, True]:
        agent.backend.generate("旧入口", request_options=ProviderRequestOptions(max_output_tokens=40000, is_probe=probe))
        assert sent[-1]["max_output_tokens"] == before == 32000
    measured = resolve_request_token_budget(input_tokens=1000, context_window_tokens=128000,
        output_ceiling=100000, shared_window=True)
    agent.backend.generate("已测量", request_options=ProviderRequestOptions(token_budget=measured))
    assert sent[-1]["max_output_tokens"] == 100000 and agent.backend.max_tokens == before
    with pytest.raises(RequestBudgetExceeded, match="invalid_budget"):
        agent.backend.generate("冲突的双cap", request_options=ProviderRequestOptions(token_budget=measured, max_output_tokens=40000))
    legacy = OpenAIResponsesBackend(BackendOptions("https://fixture.invalid/v1", "test-key", "gpt-6-test", max_tokens=32000, stream_enabled=False))
    assert legacy.configured_output_ceiling == 32000
    for invalid in [measured, replace(measured, shared_window=False), replace(measured, server_managed_output=True)]:
        with pytest.raises(RequestBudgetExceeded):
            legacy.generate("不能提升旧构造器", request_options=ProviderRequestOptions(token_budget=invalid))
    assert len(sent) == 3


@pytest.mark.parametrize("change", ["prompt", "history", "schemas", "guidance"])
def test_budget_is_frozen_before_preflight_and_rebuilt_after_input_change(monkeypatch, change):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.agent_core.tool_ir_history import native_tool_ir_history
    from agent_py_agent.agent.backends.tool_ir import UserTurn

    agent, sent = _responses_agent(monkeypatch, request_timeout=0)
    before = generation.preflight_context_pressure_response
    inspected = []

    def preflight(request):
        assert request.context_snapshot.current_tokens == request.token_budget.input_tokens
        inspected.append(request)
        return before(request)

    monkeypatch.setattr(generation, "preflight_context_pressure_response", preflight)
    first, params, snapshots = _main_response(monkeypatch, agent, 100000)
    frozen = inspected[0]
    assert generation._freeze_model_request_budget(frozen) is frozen and len(snapshots) == 1
    assert frozen.token_budget is first.request_token_budget
    if change == "prompt":
        changed = replace(frozen, prompt="实际输入已经改变")
    else:
        changed = frozen
        if change == "history":
            native_tool_ir_history(params).append(UserTurn("新增历史"))
        elif change == "schemas":
            monkeypatch.setattr(generation, "resolve_native_tools", lambda agent, params: [
                {"name": "read_file", "description": "新schema", "input_schema": {"type": "object"}}])
        else:
            monkeypatch.setattr(generation, "provider_system_instruction", lambda backend: "新增宿主指导")

    def snapshot(*args, **kwargs):
        value = replace(snapshots[0], current_tokens=200000, raw_estimated_tokens=400000,
                        prompt_tokens=200000, usage_binding=kwargs.get("usage_binding"))
        snapshots.append(value)
        return value

    monkeypatch.setattr(generation, "model_visible_context_snapshot", snapshot)
    second = generation.generate_model_response(changed)
    assert len(snapshots) == 2 and len(inspected) == 2 and len(sent) == 2
    assert second.request_token_budget is inspected[1].token_budget
    assert second.request_token_budget is not frozen.token_budget
    assert second.request_token_budget.input_tokens == 200000
    assert sent[-1]["max_output_tokens"] == second.request_token_budget.output_tokens == 69952


@pytest.mark.parametrize("kind", ["unknown", "input_only", "subscription"])
def test_unknown_input_only_and_subscription_do_not_claim_shared_window_proof(monkeypatch, kind):
    agent, sent = _responses_agent(monkeypatch, window=272000 if kind == "subscription" else 0, request_timeout=0)
    agent.backend._provider_context_window_cache = 272000
    if kind == "input_only":
        agent.backend.model_metadata = {"input_token_limit": 272000}
    if kind == "subscription":
        agent.backend.auth_ref = {"mode": "chatgpt"}
    result, _, _ = _main_response(monkeypatch, agent, 100000)
    assert not result.request_token_budget.shared_window
    assert result.request_token_budget.output_tokens == agent.backend.max_tokens
    assert result.request_token_budget.server_managed_output == (kind == "subscription")
    if kind == "subscription":
        assert "max_output_tokens" not in sent[-1]
    else:
        assert sent[-1]["max_output_tokens"] == agent.backend.max_tokens


@pytest.mark.parametrize("adapter", ["openai_compatible", "anthropic_compatible", "openai_responses"])
def test_three_http_adapters_consume_same_budget(monkeypatch, adapter):
    from copy import deepcopy

    from agent_py_agent.agent.backends import http

    agent, _ = _responses_agent(monkeypatch, model_backend=adapter, request_timeout=0)
    payloads = []

    def send(request):
        payloads.append(deepcopy(request.payload))
        if adapter == "openai_responses":
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "成功"}]}]}
        if adapter == "anthropic_compatible":
            return {"content": [{"type": "text", "text": "成功"}], "stop_reason": "end_turn"}
        return {"choices": [{"message": {"role": "assistant", "content": "成功"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(http, "post_json", send)
    response, _, snapshots = _main_response(monkeypatch, agent, 200000)
    assert response.text == "成功" and len(payloads) == len(snapshots) == 1
    cap = [payloads[0][key] for key in ("max_output_tokens", "max_tokens", "max_completion_tokens") if key in payloads[0]]
    # Responses保留实测提升；其它协议按r4保留工厂夹取的68000，但仍传递同一冻结预算。
    expected = 69952 if adapter == "openai_responses" else 68000
    assert cap == [response.request_token_budget.output_tokens] == [expected]
    assert response.request_token_budget.shared_window == (adapter == "openai_responses")
    assert agent.backend.max_tokens == 68000


def test_main_ledger_timeout_and_recovery_use_request_budget(monkeypatch):
    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.agent_core.model import call_runtime
    from agent_py_agent.agent.agent_core.tool_loop import response_decision
    from agent_py_agent.agent.backends.reasoning_control import (
        ReasoningPayloadLimits,
        effective_reasoning_wire_fields,
    )

    agent, sent = _responses_agent(monkeypatch, request_timeout=20)
    observed = []
    original = generation._effective_model_request_timeout_seconds

    def timeout(agent, first, *, token_budget=None):
        observed.append(token_budget)
        return original(agent, first, token_budget=token_budget)

    monkeypatch.setattr(generation, "_effective_model_request_timeout_seconds", timeout)
    response, _, _ = _main_response(monkeypatch, agent, 200000)
    budget = response.request_token_budget
    record = agent._model_call_ledger.records()[-1]
    assert record.input_tokens == budget.input_tokens == 200000
    assert record.output_tokens_estimate == budget.output_tokens == sent[-1]["max_output_tokens"] == 69952
    assert observed and all(item is budget for item in observed)
    assert call_runtime.effective_model_request_timeout_seconds(agent, 20, token_budget=budget) > call_runtime.effective_model_request_timeout_seconds(agent, 20)
    assert response_decision._effective_output_cap(agent, token_budget=budget) == 69952
    actual = response_decision._effective_wire_change(agent, "effort", "max", "high", token_budget=budget)
    limits = ReasoningPayloadLimits(69952, agent.backend.reasoning_levels)
    assert actual == (effective_reasoning_wire_fields("effort", "max", "responses", limits) != effective_reasoning_wire_fields("effort", "high", "responses", limits))


def test_budget_errors_enter_existing_context_pressure_and_compact_fallback_paths(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.conversation import auxiliary_model_call as auxiliary
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
    from agent_py_agent.agent.conversation.compact_remote import (
        RemoteCompactionRequest,
        request_remote_compaction,
    )
    from agent_py_agent.agent.conversation.compact_request_budget import (
        generate_bounded_compact_response,
    )
    from agent_py_agent.tests.test_compact_output_policy import _summary_request

    agent, sent = _responses_agent(monkeypatch, request_timeout=0)
    overflow, _, _ = _main_response(monkeypatch, agent, 269952)
    assert overflow.runtime_status == "context_overflow" and not sent
    small, _ = _responses_agent(monkeypatch, window=8000, request_timeout=0)
    invalid, _, _ = _main_response(monkeypatch, small, 100)
    assert invalid.runtime_reason == "COMPACT_INVALID_OUTPUT_BUDGET"
    # 上一个 helper 替换了 HTTP 接收器，重新绑定同一无网络观察点。
    agent, sent = _responses_agent(monkeypatch, request_timeout=0)
    request = _summary_request(agent)
    history = [{"role": "user", "content": "x" * 900000}]
    source = CompactMessageSource(lambda: iter(history), preserve_prefix=True)
    with pytest.raises(ConversationCompactError) as error:
        generate_bounded_compact_response(replace(request, messages=None), message_source=source)
    assert error.value.code == "COMPACT_PREFIX_TOO_LARGE" and not sent
    with pytest.raises(ConversationCompactError) as error:
        generate_bounded_compact_response(request, vision_summary=True, media_reserve_tokens=300000)
    assert error.value.code == "COMPACT_VISION_SUMMARY_FAILED" and not sent
    assert request_remote_compaction(RemoteCompactionRequest(agent, "前缀", history)) is None
    assert history[0]["content"] == "x" * 900000 and not sent
    monkeypatch.setattr(auxiliary, "is_interrupted", lambda: True)
    with pytest.raises(InterruptedError):
        request_remote_compaction(RemoteCompactionRequest(agent, "前缀", request.messages))
    assert not sent


# LLM: 传输基类不能发起模型生成，不因拥有HTTP字段就升级成共享窗口适配器。
# 函数用途: 保留纯容量投影后端的百分比触发线。
def test_http_transport_without_generation_has_no_shared_budget_proof():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.model.call_runtime import request_budget_shared_window
    from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.http import HttpBackend

    agent = SimpleNamespace(backend=HttpBackend(BackendOptions("https://fixture.invalid", "fixture", "fixture",
        max_tokens=4000, context_window_tokens=10000)), config=SimpleNamespace(model_context_window_tokens=10000))
    assert not request_budget_shared_window(agent)
    assert 0 < runtime_compact_policy(agent).trigger_tokens <= 10000


# LLM: 当前请求配置只能收紧构造时的天花板，不修改共享后端，也不恢复被供应商降低的上限。
# 函数用途: 防止配置已降低输出上限而实测规划仍发送旧值。
def test_measured_ceiling_obeys_current_lowered_configuration(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.model.call_runtime import model_request_token_budget

    agent, sent = _responses_agent(monkeypatch)
    agent.config = replace(agent.config, max_tokens=50000)
    before = (agent.backend.max_tokens, agent.backend.configured_output_ceiling)
    budget = model_request_token_budget(agent, input_tokens=1000)
    assert budget.output_tokens == 50000
    assert (agent.backend.max_tokens, agent.backend.configured_output_ceiling) == before
    assert not sent


# LLM: 未冻结候选保留旧输出预留；已测量的主请求只沿冻结规划判断，不能再减一遍完整天花板。
# 函数用途: 验证两种准入入口不会互相覆盖预算合同。
def test_input_ceiling_distinguishes_unmeasured_and_frozen_requests(monkeypatch):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_request_token_budget
    from agent_py_agent.agent.agent_core.model.context_pressure import model_request_input_ceiling

    agent, sent = _responses_agent(monkeypatch)
    budget = model_request_token_budget(agent, input_tokens=200000)
    assert model_request_input_ceiling(agent, 272000) == 272000 - agent.backend.max_tokens
    assert model_request_input_ceiling(agent, 272000, token_budget=budget) == 272000 - budget.safety_tokens
    assert not sent


# LLM: 预算错误保留准确码和可调整策略，但绝不因登记分类自动重放同一请求。
# 函数用途: 验证输出预算错误在正式错误目录有明确、非重放恢复建议。
def test_budget_error_has_registered_non_replaying_recovery():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.contracts.recovery import RecoveryAction

    contract = error_contract("REQUEST_OUTPUT_BUDGET_UNAVAILABLE")
    assert contract.code == "REQUEST_OUTPUT_BUDGET_UNAVAILABLE"
    assert contract.category == "resource" and not contract.retryable
    assert contract.recommended_action == RecoveryAction.CHANGE_STRATEGY.value


# LLM: 配置明确清空窗口后，旧backend的构造值不再是当前请求的共享容量证明。
# 函数用途: 防止清除配置仍被旧窗口升级成实测规划。
def test_cleared_window_does_not_reuse_stale_backend_shared_proof(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.model.call_runtime import (
        model_request_token_budget,
        request_budget_shared_window,
    )

    agent, sent = _responses_agent(monkeypatch)
    assert agent.backend.configured_context_window_tokens == 272000
    agent.config = replace(agent.config, model_context_window_tokens=None)
    assert not request_budget_shared_window(agent)
    budget = model_request_token_budget(agent, input_tokens=1000)
    assert not budget.shared_window and budget.context_window_tokens == 0
    assert budget.output_tokens == agent.backend.max_tokens and not sent
