# LLM: 覆盖中途摘要失败的原入口；只替换模型/计量边界，不访问真实供应商，canonical IR与Store仍作独立事实核对。
# 模块用途: 验证两级续跑、四字段硬停、增长重试、可校验桩及出站身份/持久化隔离。
from __future__ import annotations

import asyncio
import builtins
import json
import logging
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as service
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.model import context_pressure
from agent_py_agent.agent.agent_core.tool_ir_history import project_native_prompt_history
from agent_py_agent.agent.backends.errors import ProviderQuotaExhaustedError, ProviderTransientError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, CompactionSummary, UserTurn
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolContentBlock,
    ToolProtocolSnapshot,
    ToolResult,
    ToolResultRef,
)


# LLM: 使用真实frozen循环参数、typed调用/结果及真实模型投影；所有正文均为本地合成样本。
# 函数用途: 构造有十对工具往返、用户插话和已有摘要的单回合，不借用其它run的可变状态。
def loop_params() -> ToolLoopExecuteParams:
    history = [CompactionSummary("pinned previous summary", source="applied_compact"), UserTurn("keep current user")]
    for i in range(10):
        call = ToolCall(call_id=f"c{i}", tool_name="read_file", arguments={"path": f"file{i}.txt"},
                        source_protocol="native", schema_hash="sha256:fixture", run_id="run-1",
                        turn_id="turn-1", attempt_id="attempt-1")
        history.extend((AssistantTurn(tool_calls=[call]), ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, status="succeeded",
            content_blocks=(ToolContentBlock("text", text="x" * 1000),),
            refs=(ToolResultRef("artifact", f"archive:c{i}"),),
        )))
    return ToolLoopExecuteParams(
        user_prompt="keep current user", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[],
        effective_on_chunk=None, allowed_tools=None, write_boundary=None, task_attributes=None,
        request_id="request-1", run_id="run-1", task_id="task-1", one_shot_tool_calls=set(),
        executed_tools=[], archive_tool_calls=[], tool_ir_history=history,
        tool_protocol_snapshot=ToolProtocolSnapshot("run-1", "native", ProviderToolCapability(
            "fixture", "local://fixture", "fixture", False, True, "test-native")),
    )


# LLM: 仅替换昂贵模型入口和完整请求估算边界；降级、请求投影、阈值与所有权由真实产品代码执行。
# 函数用途: 用确定性计量让样本落在指定安全线，摘要尝试次数独立记录。
def arrange(monkeypatch, error, *, tokens=900):
    params = loop_params()
    calls = []
    policy = SimpleNamespace(context_window_tokens=1000, trigger_tokens=700,
                             recent_tail_tokens=100, recovery_target_tokens=600, allow_persistent_apply=True)
    agent = SimpleNamespace(config=SimpleNamespace(), backend=SimpleNamespace())
    monkeypatch.setattr(service, "_native_compact_policy", lambda *a: policy)
    monkeypatch.setattr(context_pressure, "compact_request_calibration_for_request", lambda *a: None)
    # 同一原生投影的内容决定是否真的腾出了容量，不能靠改计量数字制造通过。
    # LLM: 仿真完整请求计量边界，数值必须随真实投影的结果体积变化。
    # 函数用途: 为阈值测试提供可复现的token估算，不伪造桩已经生效。
    def snapshot(agent, projected_params, prompt):
        _, history = project_native_prompt_history(projected_params, prompt)
        size = sum(len(r.output) for r in history if isinstance(r, ToolResult))
        return SimpleNamespace(raw_estimated_tokens=tokens - (10000 - size) // 10)
    monkeypatch.setattr(context_pressure, "model_visible_context_snapshot", snapshot)
    # LLM: 只替换摘要准备模型边界，记录force让软/硬入口可独立核对。
    # 函数用途: 产生一次可控摘要错误，并暴露不应重复发生的尝试。
    def prepare(*a, **kw):
        calls.append(kw["force"])
        raise error
    monkeypatch.setattr(service, "_prepare_native_compact_plan", prepare)
    return agent, params, calls, snapshot


# LLM: 软失败不改变原IR；70%以上按返修合同做局部模型投影，等于92%仍可继续，同轮replace不重复摘要。
# 函数用途: 核对四类摘要失败的Level1局部缩减、边界和锁存，保留原历史相等断言。
@pytest.mark.parametrize("code", ["COMPACT_FAILED", "COMPACT_SUMMARY_EMPTY", "COMPACT_BUDGET_EXCEEDED", "COMPACT_TIMEOUT"])
@pytest.mark.parametrize("tokens", [900, 920])
def test_soft_summary_failure_continues_unchanged_and_latches(monkeypatch, caplog, code, tokens):
    agent, params, calls, _ = arrange(monkeypatch, ConversationCompactError("summary", code=code), tokens=tokens)
    before = deepcopy(params.tool_ir_history)
    with caplog.at_level(logging.WARNING):
        try:
            result = service._fit_native_ir_to_shared_budget(agent, params, "system")
        except ConversationCompactError:
            pytest.fail("degradable summary failure aborted an otherwise safe active turn")
    assert result > 0
    assert params.tool_ir_history == before
    assert params.compact_degradation_latched is True
    assert any(getattr(r, "compact_degraded_level", None) == 1 and r.reason == code for r in caplog.records)
    service._fit_native_ir_to_shared_budget(agent, replace(params, tool_rounds=2), "system")
    assert calls == [False]


# LLM: Level2只在模型副本改结果正文，源IR、refs、调用和最新四对必须原样保留。
# 函数用途: 验证近窗、超窗和强制失败均可确定性腾出85%以内预算。
@pytest.mark.parametrize("force,tokens", [(False, 921), (False, 1100), (True, 900), (True, 800)])
def test_level_two_only_projects_old_results_preserving_tail_refs_and_raw_history(monkeypatch, caplog, force, tokens):
    agent, params, calls, snapshot = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"), tokens=tokens)
    before = deepcopy(params.tool_ir_history)
    # 持久化入口若被误用会失败；另核对原IR不被替换，防止finalization随后保存桩。
    monkeypatch.setattr(service, "_commit_native_ir_generation", lambda *a, **k: pytest.fail("turn-local trim advanced persistent pin"))
    with caplog.at_level(logging.WARNING):
        try:
            service._fit_native_ir_to_shared_budget(agent, params, "system", force=force)
        except ConversationCompactError:
            pytest.fail("near-ceiling transient summary failure did not use deterministic projection")
    assert params.tool_ir_history == before
    _, projected = project_native_prompt_history(params, "system")
    original_results = [r for r in before if isinstance(r, ToolResult)]
    projected_results = [r for r in projected if isinstance(r, ToolResult)]
    assert projected_results[-4:] == original_results[-4:]
    assert [r.call_id for r in projected_results] == [r.call_id for r in original_results]
    trimmed = [(a, b) for a, b in zip(original_results, projected_results, strict=True) if a != b]
    assert trimmed
    for raw, stub in trimmed:
        data = json.loads(stub.output)
        assert data["call_id"] == raw.call_id
        assert data["tool_name"] == raw.tool_name
        assert data["status"] == raw.status
        assert data["char_length"] == len(raw.output)
        assert data["archive_ref"] == raw.refs[0].ref
        assert stub.refs == raw.refs
    assert projected[:2] == before[:2]
    assert snapshot(agent, params, "system").raw_estimated_tokens <= 850
    assert any(getattr(r, "compact_degraded_level", None) == 2 for r in caplog.records)
    assert calls == [force]


# LLM: 硬停对照不能被软错误捕获扩大影响，必须传播同一异常对象。
# 函数用途: 验证pin/owner/配额/取消等失败不改变IR或降级标记。
@pytest.mark.parametrize("error", [
    ConversationCompactError("pin", code="COMPACT_PIN_CONFLICT"),
    ConversationCompactError("owner", code="COMPACT_OWNER_MISMATCH"),
    ConversationCompactError("quota", code="COMPACT_INSUFFICIENT_QUOTA"),
    ProviderQuotaExhaustedError("no budget"), ToolCancelled("stop"),
    asyncio.CancelledError(), KeyboardInterrupt(),
])
def test_hard_failures_propagate_without_latch_or_mutation(monkeypatch, error):
    agent, params, calls, _ = arrange(monkeypatch, error)
    before = deepcopy(params.tool_ir_history)
    with pytest.raises(type(error)) as caught:
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert caught.value is error
    assert not getattr(params, "compact_degradation_latched", False)
    assert params.tool_ir_history == before
    assert calls == [False]


# LLM: 摘要后端真实瞬时异常不一定先包装为COMPACT错误，同样只能在准备阶段降级。
# 函数用途: 验证typed ProviderTransientError不会误杀仍有容量的工具回合。
def test_transient_provider_failure_uses_same_continuation(monkeypatch):
    agent, params, calls, _ = arrange(monkeypatch, ProviderTransientError("temporary"))
    try:
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    except ProviderTransientError:
        pytest.fail("transient summary provider failure aborted the active turn")
    assert params.compact_degradation_latched
    assert calls == [False]


# LLM: 正常选模和Compact恢复都要冻结输入；冻结后的纯请求必须仍带本轮投影，不能复活旧大正文。
# 函数用途: 直接核对真实capture→project链路，避免只测临时列表而漏掉真正发送路径。
def test_frozen_request_preserves_degraded_view_but_keeps_canonical_ir(monkeypatch):
    from agent_py_agent.agent.agent_core.tool_request_capture import capture_tool_loop_request
    from agent_py_agent.agent.agent_core.tool_request_projection import project_tool_loop_request
    from agent_py_agent.agent.conversation.models import ConversationHistorySeed
    from agent_py_agent.agent.prompting_parts.builder import PromptRenderInput

    agent, params, _, _ = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"), tokens=980)
    params = replace(params, conversation_history_seed=ConversationHistorySeed())
    original = deepcopy(params.tool_ir_history)
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    prompt_input = PromptRenderInput(
        system_prompt="system", memory_text="", owner_scope="", dynamic="", workspace_context="",
        injection_fragments=(), tool_catalog="", recommendations="", execution_facts="",
        user_prompt=params.user_prompt, task_and_transcript="", native_tool_use=True,
    )
    frozen = capture_tool_loop_request(agent, params, prompt_input)
    projected = project_tool_loop_request(frozen)
    assert projected.status == "ready"
    assert "char_length" in json.dumps(projected.messages)
    assert frozen.tool_ir_history == tuple(original)
    params.compact_degradation_stubs.clear()
    assert "char_length" in json.dumps(project_tool_loop_request(frozen).messages)


# LLM: 共享宿主回退必须先核验相同分类；fake只替换种子重建，不得绕过权限/配额的硬停。
# 函数用途: 在实际optional恢复入口复现pin/owner冲突被误认为可选摘要失败的问题。
@pytest.mark.parametrize("code", ["COMPACT_PIN_CONFLICT", "COMPACT_OWNER_MISMATCH", "COMPACT_AUTHORITY_CONFLICT"])
def test_host_preflight_never_restores_hard_authority_failures(monkeypatch, code):
    from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
    from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestInput

    agent, params, _, _ = arrange(monkeypatch, ConversationCompactError("not used", code="COMPACT_FAILED"))
    params = replace(params, conversation_history_seed=object())
    host = SimpleNamespace(force=False, agent=agent, calibration=None,
                           source=SimpleNamespace(policy=SimpleNamespace(context_window_tokens=1000)))
    monkeypatch.setattr(recovery, "_native_provider_history_messages", lambda *a, **k: [])
    assert recovery._restore_optional_preflight(host, params, ToolLoopRequestInput(), 800,
        ConversationCompactError("must stop", code=code)) is False


# LLM: 全图中任何真实硬错误都优先，外层COMPACT_FAILED或重试类不能掩盖它；不读取异常文案。
# 函数用途: 检查cause/context硬停与未知失败保守策略，以及消息文字不能假装配额错误。
@pytest.mark.parametrize("hard", [
    ConversationCompactError("pin", code="COMPACT_PIN_CONFLICT"),
    ProviderQuotaExhaustedError("no quota"), ToolCancelled("stop"), asyncio.CancelledError(),
])
def test_shared_classifier_preserves_chained_hard_stop(hard):
    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    outer = ConversationCompactError("transient wrapper", code="COMPACT_FAILED")
    outer.__cause__ = hard
    assert compact_failure_is_degradable(outer)[0] is False
    outer.__cause__ = None
    outer.__context__ = hard
    assert compact_failure_is_degradable(outer)[0] is False


# LLM: 已成功CAS后的刷新错误不是摘要准备失败，必须传播，不能借新降级逻辑回滚或重放。
# 函数用途: 保证异常捕获不扩到_apply及其提交后阶段。
def test_failure_after_plan_preparation_is_not_degraded(monkeypatch):
    agent, params, calls, _ = arrange(monkeypatch, ConversationCompactError("unused", code="COMPACT_FAILED"))
    monkeypatch.setattr(service, "_prepare_native_compact_plan", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(service, "replace", lambda value, **k: value)
    error = ConversationCompactError("post commit failure", code="COMPACT_FAILED")
    # LLM: 模拟提交/刷新失败，不参与摘要准备替身。
    # 函数用途: 确保捕获边界没有吞掉提交阶段的原始异常。
    def apply(*a):
        raise error
    monkeypatch.setattr(service, "_apply_native_compact_plan", apply)
    with pytest.raises(ConversationCompactError) as caught:
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert caught.value is error
    assert not params.compact_degradation_latched
    assert calls == []


# LLM: 降级锁存不能跨新参数构造或Owner流转；已锁存时仍须响应取消。
# 函数用途: 验证新回合重置和同轮取消优先，不把失败状态放进共享Agent。
def test_latch_is_turn_local_and_cancellation_still_stops(monkeypatch):
    agent, params, _, _ = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"))
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert not loop_params().compact_degradation_latched
    assert loop_params().compact_degradation_stubs == {}
    params.cancellation_token.cancel("user stop")
    with pytest.raises(InterruptedError):
        service._fit_native_ir_to_shared_budget(agent, params, "system")


# LLM: 四对尾部属于必须保护的来源，不允许为制造成功删掉它们；失败后派生覆盖也必须回滚。
# 函数用途: 最小安全材料仍过大时明确失败，保留原IR和原投影。
def test_untrimmable_request_fails_closed_without_deleting_recent_pairs(monkeypatch):
    agent, params, _, _ = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"), tokens=2500)
    before = deepcopy(params.tool_ir_history)
    with pytest.raises(ConversationCompactError, match="安全预算"):
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert params.tool_ir_history == before
    assert params.compact_degradation_stubs == {}


# LLM: Gateway overflow信号已有完整原turn携带；soft preflight不能被误升级为provider硬溢出。
# 函数用途: 核对真实Gateway恢复安装入口传递正确force，不重跑业务也不改加载/CAS合同。
@pytest.mark.parametrize("source,forced", [("preflight", False), ("provider_error", True)])
def test_gateway_distinguishes_soft_preflight_from_provider_overflow(monkeypatch, source, forced):
    from agent_py_agent.agent import gateway_compact_recovery
    from agent_py_agent.agent.gateway_parts import request_context, request_execution

    agent = SimpleNamespace()
    context = SimpleNamespace(agent=agent, request={})
    load = request_context.GatewayConversationLoadRequest(agent, {}, "request", "prompt")
    refreshed = SimpleNamespace(compact_source=SimpleNamespace(messages=("source",)), compact_generation=0)
    observer = SimpleNamespace(compact_recovery=None)
    seen = []
    monkeypatch.setattr(request_execution, "build_gateway_compact_load_request", lambda *a: load)
    monkeypatch.setattr(request_context, "gateway_conversation_context", lambda *a, **k: refreshed)
    monkeypatch.setattr(request_execution, "_require_gateway_conversation_ready", lambda *a: None)
    # LLM: 只旁听恢复安装时传出的force，不做模型或Store工作。
    # 函数用途: 记录软预检和provider溢出是否被准确区分。
    def prepare(*a, **k):
        seen.append(k.get("force", True))
        return "prepared"
    monkeypatch.setattr(gateway_compact_recovery, "prepare_gateway_compact_recovery", prepare)
    run = SimpleNamespace(compact_trigger_source=source, native_compact_carry=None)
    assert request_execution._gateway_compact_overflowing_turn(context, "prompt", refreshed, run, [], observer=observer) is refreshed
    assert observer.compact_recovery == "prepared"
    assert seen == [forced]


# LLM: shared恢复只借用原种子及冻结请求；强制摘要失败必须获得真实更小的模型视图，不能改变canonical材料。
# 函数用途: 复现强制/近窗宿主拒绝本可纯内存折叠的工具结果，核对完整冻结请求随之更新。
@pytest.mark.parametrize("forced", [False, True])
def test_shared_host_can_project_near_ceiling_failure_without_summary_commit(monkeypatch, forced):
    from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
    from agent_py_agent.agent.agent_core.tool_request_capture import capture_tool_loop_request
    from agent_py_agent.agent.conversation.models import ConversationHistorySeed
    from agent_py_agent.agent.prompting_parts.builder import PromptRenderInput

    agent, params, _, snapshot = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"), tokens=980)
    params = replace(params, conversation_history_seed=ConversationHistorySeed())
    prompt_input = PromptRenderInput("system", "", "", "", "", (), "", "", "", "user", "", True)
    frozen = capture_tool_loop_request(agent, params, prompt_input)
    original = deepcopy(params.tool_ir_history)
    host = SimpleNamespace(force=forced, agent=agent, calibration=None,
                           source=SimpleNamespace(policy=SimpleNamespace(context_window_tokens=1000)),
                           resolved_input=None, committed=False)
    monkeypatch.setattr(recovery, "_native_provider_history_messages", lambda *a, **k: [])
    assert recovery._restore_optional_preflight(host, params, frozen, 980,
        ConversationCompactError("summary", code="COMPACT_FAILED"))
    assert not host.committed
    assert params.tool_ir_history == original
    assert host.resolved_input.tool_ir_history == tuple(original)
    assert snapshot(agent, params, "system").raw_estimated_tokens <= 850
    assert params.compact_degradation_latched


# LLM: 真实发送前的materializer会把投影写回IR；临时降级必须穿过它而不把桩变成持久历史。
# 函数用途: 对照原始IR、实际后端消息与正式收口序列化，覆盖只测纯投影时看不到的反写。
def test_real_materialization_and_finalization_keep_original_tool_payloads(monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.loop_support import _completed_turn_native_messages
    from agent_py_agent.agent.agent_core.tool_model_generation import (
        ModelGenerateParams,
        _materialize_native_prompt_facts,
        _native_provider_messages,
    )
    from agent_py_agent.agent.backends.base import ModelResponse

    agent, params, _, _ = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"), tokens=980)
    before = deepcopy(params.tool_ir_history)
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    _materialize_native_prompt_facts(ModelGenerateParams(agent, params, "system", 0))
    assert params.tool_ir_history == before, "provider-only stubs leaked into canonical materialization"
    actual = _native_provider_messages(agent, params)
    assert "char_length" in json.dumps(actual)
    canonical = _completed_turn_native_messages(params, ModelResponse(text="done", backend="fixture"))
    serialized = json.dumps(canonical)
    assert "char_length" not in serialized
    assert "x" * 1000 in serialized


# LLM: 使用真实绑定/失败熔断及真实会话根目录；只把LLM摘要与固定开销计量换成确定性边界。
# 函数用途: 在有canonical线程的真实规划路径触发降级，核对消息JSONL与summary pin/代次不变。
def test_real_bound_thread_jsonl_and_summary_pin_are_unchanged(monkeypatch, tmp_path):
    from agent_py_agent.agent.agent_core import _finalization_service
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
    )
    from agent_py_agent.tests.test_gateway_compact_recovery import _history_request

    fixture = _history_request(tmp_path)
    agent = fixture.agent
    prepare = service._prepare_native_compact_plan
    _, params, _, _ = arrange(monkeypatch, ConversationCompactError("unused", code="COMPACT_FAILED"), tokens=980)
    params = replace(params, save=True, task_attributes={
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": fixture.thread_id,
    })
    root = agent.conversation_store.storage.root
    jsonl_before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.jsonl")}
    assert jsonl_before
    thread_before = agent.conversation_store.threads.require(fixture.thread_id)
    monkeypatch.setattr(service, "_prepare_native_compact_plan", prepare)
    monkeypatch.setattr(service, "_native_compact_floor_tokens", lambda *a: 100)
    monkeypatch.setattr(_finalization_service, "_request_memory_curator", lambda *a: None)
    attempts = []
    # LLM: 真实绑定已经完成，只在摘要模型边界制造瞬时失败。
    # 函数用途: 让线程失败记账执行，但不创建真实LLM请求。
    def summary(*a, **k):
        attempts.append(True)
        raise ConversationCompactError("temporary summary failure", code="COMPACT_SUMMARY_EMPTY")
    monkeypatch.setattr(service, "_native_tool_history_summary", summary)
    monkeypatch.setattr(service, "_commit_native_ir_generation", lambda *a, **k: pytest.fail("degradation reached persistent commit"))
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert attempts == [True]
    assert params.compact_degradation_latched
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.jsonl")} == jsonl_before
    thread_after = agent.conversation_store.threads.require(fixture.thread_id)
    assert thread_after.summary == thread_before.summary
    assert thread_after.compact_generation == thread_before.compact_generation
    assert thread_after.compact_checkpoint_id == thread_before.compact_checkpoint_id


# LLM: 外层soft不能把未知/权限异常洗成可恢复，异常组各分支都必须安全。
# 函数用途: 实证独立复核提出的cause/context及混合异常组反例，连同native入口一起拒绝。
@pytest.mark.parametrize("cause", [PermissionError("denied"), RuntimeError("unknown"),
    builtins.ExceptionGroup("mixed", [TimeoutError(), RuntimeError("unknown")])])
def test_unknown_or_permission_cause_cannot_be_laundered_by_soft_wrapper(monkeypatch, cause):
    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    outer = ConversationCompactError("wrapper", code="COMPACT_FAILED")
    outer.__cause__ = cause
    assert compact_failure_is_degradable(outer)[0] is False
    agent, params, _, _ = arrange(monkeypatch, outer)
    with pytest.raises(ConversationCompactError) as caught:
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert caught.value is outer
    assert not params.compact_degradation_latched
    outer.__cause__, outer.__context__ = None, cause
    assert compact_failure_is_degradable(outer)[0] is False


# LLM: fit原样续跑后，紧接的真实preflight也必须放行，不能又以旧软阈值合成overflow。
# 函数用途: 验证Level1与窗口告警的实际生成前门协同，精确92%仍继续且消费旧窗口信号。
@pytest.mark.parametrize("tokens", [900, 920])
@pytest.mark.parametrize("window_signal", [False, True])
def test_latched_turn_passes_actual_preflight_instead_of_restarting(monkeypatch, tokens, window_signal):
    from agent_py_agent.agent.agent_core.tool_model_generation import ModelGenerateParams

    agent, params, _, snapshot = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"), tokens=tokens)
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    policy = service._native_compact_policy(agent, params)
    monkeypatch.setattr(context_pressure, "runtime_compact_policy", lambda *a, **k: policy)
    monkeypatch.setattr(context_pressure, "model_visible_context_tokens", lambda a, p, prompt: snapshot(a, p, prompt).raw_estimated_tokens)
    if window_signal:
        params.live_archive_state["tool_context_window_overflow"] = {"omitted_count": 1}
    request = ModelGenerateParams(agent, params, "system", 0)
    assert context_pressure.preflight_context_pressure_response(request) is None
    assert "tool_context_window_overflow" not in params.live_archive_state
    # 同一锁存仍不能越过92%安全线或真实输入上限。
    monkeypatch.setattr(context_pressure, "model_visible_context_tokens", lambda *a: 1000)
    assert context_pressure.preflight_context_pressure_response(request) is not None


# LLM: call_id只在原turn内有意义；不同turn重复ID不能让旧桩替换受保护尾部的另一条执行事实。
# 函数用途: 实证裸ID覆盖风险，用不同四元来源但相同call_id验证尾四对仍为原件。
def test_repeated_call_id_in_later_turn_does_not_replace_protected_tail(monkeypatch):
    agent, params, _, _ = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"), tokens=980)
    last_call = params.tool_ir_history[-2].tool_calls[0]
    params.tool_ir_history[-2] = AssistantTurn(tool_calls=[replace(last_call, call_id="c0", turn_id="later-turn")])
    params.tool_ir_history[-1] = replace(params.tool_ir_history[-1], call_id="c0")
    before = deepcopy(params.tool_ir_history)
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    _, projected = project_native_prompt_history(params, "system")
    assert [r for r in projected if isinstance(r, ToolResult)][-4:] == [r for r in before if isinstance(r, ToolResult)][-4:]
    assert params.tool_ir_history == before


# LLM: SDK的code和本仓error_code都是结构化事实；即便类型归transient也不能覆盖明确硬配额/认证码。
# 函数用途: 防止第三方异常仅在code字段报告硬停时被误认为瞬时限流。
@pytest.mark.parametrize("code", ["insufficient_quota", "quota_exceeded", "billing", "authentication_error", "invalid_api_key"])
def test_provider_code_attribute_also_blocks_degradation(code):
    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    error = ProviderTransientError("provider structured rejection")
    error.code = code
    assert compact_failure_is_degradable(error)[0] is False


# LLM: 本地fake LLM驱动真实SimpleAgent与工具循环，所有文件均在pytest临时目录，不调用外部模型。
# 函数用途: 核对真实循环的摘要/采样/工具次数；第二组跨多轮证明基线与15%增长确实随实际返回参数传播。
@pytest.mark.parametrize("tool_count,pressures,summary_after,summary_count", [
    (2, (60_000, 60_000, 90_000), 2, 1),
    (4, (60_000, 80_000, 90_000, 92_000, 92_000), 1, 2),
])
def test_real_tool_loop_continues_without_replaying_tools(monkeypatch, tmp_path, tool_count, pressures, summary_after, summary_count):
    from pathlib import Path

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.registry_workspace import effective_registry_cwd

    file_names = ["first.txt", "second.txt", "third.txt", "fourth.txt"][:tool_count]
    # LLM: 只模拟模型响应，能力以typed probe声明，真实文件读取仍经过正式工具Gateway。
    # 类用途: 返回指定次数的不同读取和一条收口，捕获业务模型调用次数。
    class NativeBackend:
        name = "capture"

        # LLM: 无网络、无探测副作用；只声明这个测试后端能接受原生工具消息。
        # 函数用途: 允许真实运行链冻结native协议。
        def probe_tool_capability(self):
            return ProviderToolCapability("capture", "local://fixture", "fixture", False, True, "test-native")

        # LLM: 每个实例独占调用账，不在Owner或测试间共享。
        # 函数用途: 初始化业务调用计数。
        def __init__(self):
            self.calls = []

        # LLM: 只生成结构化read_file请求，达到指定次数后收口；不直接执行或写工具结果。
        # 函数用途: 让被测代理独立完成真实读取，检查原始用例及增长重试用例。
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls.append(kwargs.get("messages"))
            if len(self.calls) <= tool_count:
                name = file_names[len(self.calls) - 1]
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                    "id": f"call-{len(self.calls)}", "name": "read_file", "input": {"path": name},
                }])
            return ModelResponse(text="本轮已核对完成。", backend=self.name)

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                   model_context_window_tokens=100_000, auto_save_memory=False), tmp_path)
    cwd = Path(effective_registry_cwd(agent.tools.workspace_root, None)).resolve()
    assert cwd.is_relative_to(tmp_path.resolve())
    cwd.mkdir(parents=True, exist_ok=True)
    for name in file_names:
        (cwd / name).write_text(f"{name} result", encoding="utf-8")
    backend = NativeBackend()
    agent.backend = backend
    summaries = []
    original_snapshot = context_pressure.model_visible_context_snapshot

    # LLM: 保留完整真实snapshot形状，只模拟容量序列，增长用例的80000→92000恰好15%。
    # 函数用途: 让真实生成前门和降级入口看到同一个可复现token计量。
    def snapshot(a, p, prompt, **kwargs):
        value = original_snapshot(a, p, prompt, **kwargs)
        tokens = pressures[min(len(getattr(p, "executed_tools", ())), len(pressures) - 1)]
        return replace(value, current_tokens=tokens, raw_estimated_tokens=tokens)

    # LLM: 到指定工具数才在摘要准备边界失败；锁存应抑制重复，仅增长15%才再试。
    # 函数用途: 观察真实主循环的摘要尝试，不替代理执行工具。
    def prepare(a, p, prompt, **kwargs):
        if len(p.executed_tools) >= summary_after:
            summaries.append(True)
            raise ConversationCompactError("temporary summary failure", code="COMPACT_FAILED")
        return None

    monkeypatch.setattr(context_pressure, "model_visible_context_snapshot", snapshot)
    monkeypatch.setattr(context_pressure, "model_visible_context_tokens", lambda a, p, prompt: snapshot(a, p, prompt).current_tokens)
    monkeypatch.setattr(service, "_prepare_native_compact_plan", prepare)
    result = agent.run(f"分别读取{'、'.join(file_names)}，然后说明已核对。", params=RunParams(save=False))
    assert result.response == "本轮已核对完成。"
    assert len(backend.calls) == tool_count + 1
    assert len(result.executed_tools) == tool_count, result.archive_tool_calls
    assert summaries == [True] * summary_count
    assert [r["parameters"]["path"] for r in result.archive_tool_calls if r.get("tool") == "read_file"] == file_names


# LLM: 标准库HTTPError也是URLError子类，但永久4xx不属于临时网络故障；仅429/5xx允许降级。
# 函数用途: 防止统一网络错误处理把明确请求/配置错误洗成瞬断。
@pytest.mark.parametrize("status,allowed", [(429, True), (503, True), (400, False), (404, False), (422, False)])
def test_http_transport_status_still_distinguishes_transient_from_permanent(status, allowed):
    from urllib.error import HTTPError

    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    error = HTTPError("local://fixture", status, "test status", None, None)
    assert compact_failure_is_degradable(error)[0] is allowed


# LLM: 四个结构化码字段都须先于transient类型裁决；不从异常文字推断权限。
# 函数用途: 对照大小写、前缀和字段验证硬停码不能借重试类进入降级。
@pytest.mark.parametrize("field", ["code", "error_code", "reason_code", "failure_reason"])
@pytest.mark.parametrize("code", [
    form
    for name in ("CAS_CONFLICT", "SCOPE_MISMATCH", "OWNER_MISMATCH", "LEDGER_CORRUPT",
                 "CANCELLED", "USER_CANCELLED", "AUTH_ERROR", "PERMISSION_DENIED")
    for value in (name, f"COMPACT_{name}")
    for form in (value, value.lower())
])
def test_round_two_structured_hard_codes_override_transient_class(monkeypatch, field, code):
    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    error = ProviderTransientError("probe")
    setattr(error, field, code)
    assert compact_failure_is_degradable(error)[0] is False
    agent, params, calls, _ = arrange(monkeypatch, error)
    before = deepcopy(params.tool_ir_history)
    with pytest.raises(ProviderTransientError) as caught:
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert caught.value is error
    assert calls == [False]
    assert not params.compact_degradation_latched
    assert params.tool_ir_history == before
    assert not params.compact_degradation_stubs


# LLM: 这三类已有结构化摘要故障是可降级事实；小请求不需要局部折叠。
# 函数用途: 在真实fit入口核对补齐软码后不再把仍安全的回合中断。
@pytest.mark.parametrize("code", [
    form
    for name in ("MODEL_CALL_FAILED", "PARSE_FAILED", "BACKEND_UNAVAILABLE")
    for value in (name, f"COMPACT_{name}")
    for form in (value, value.lower())
])
def test_round_two_missing_soft_codes_continue_safely(monkeypatch, code):
    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    error = ConversationCompactError("summary", code=code)
    assert compact_failure_is_degradable(error)[0] is True
    agent, params, calls, _ = arrange(monkeypatch, error, tokens=650)
    before = deepcopy(params.tool_ir_history)
    assert service._fit_native_ir_to_shared_budget(agent, params, "system") == 0
    assert params.compact_degradation_latched
    assert params.tool_ir_history == before
    assert calls == [False]


# LLM: 只替换完整请求计量边界；阈值前后应由实际fit控制摘要次数，普通锁存续轮不能移动基线。
# 函数用途: 核对15%精确边界、整数上取整、再次失败更新基线及新run重置。
@pytest.mark.parametrize("baseline,threshold", [(800, 920), (760, 874), (799, 919)])
def test_round_two_growth_unlocks_remote_summary_at_exact_boundary(monkeypatch, baseline, threshold):
    agent, params, calls, _ = arrange(monkeypatch, ConversationCompactError("summary", code="COMPACT_FAILED"))
    pressure = [baseline]
    monkeypatch.setattr(context_pressure, "model_visible_context_snapshot",
                        lambda *a, **k: SimpleNamespace(raw_estimated_tokens=pressure[0]))
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    continued = replace(params, tool_rounds=2)
    pressure[0] = threshold - 1
    service._fit_native_ir_to_shared_budget(agent, continued, "system")
    assert calls == [False]
    pressure[0] = threshold
    service._fit_native_ir_to_shared_budget(agent, continued, "system")
    assert calls == [False, False]
    assert continued.compact_degraded_baseline_tokens == threshold
    service._fit_native_ir_to_shared_budget(agent, continued, "system")
    assert calls == [False, False]
    assert continued.compact_degraded_baseline_tokens == threshold
    assert loop_params().compact_degraded_baseline_tokens == 0


# LLM: 新尝试遇明确硬停要传播原异常；旧latch不能在下一轮绕开新CAS错误。
# 函数用途: 核对增长重新摘要后的硬错误不会再进入局部降级。
def test_round_two_growth_reattempt_hard_failure_clears_old_latch(monkeypatch):
    error = ProviderTransientError("probe")
    agent, params, calls, _ = arrange(monkeypatch, error)
    pressure = [800]
    monkeypatch.setattr(context_pressure, "model_visible_context_snapshot",
                        lambda *a, **k: SimpleNamespace(raw_estimated_tokens=pressure[0]))
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    error.reason_code = "CAS_CONFLICT"
    pressure[0] = 920
    with pytest.raises(ProviderTransientError) as caught:
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert caught.value is error
    assert calls == [False, False]
    assert not params.compact_degradation_latched


# LLM: 用户停止先于增长解锁，不允许为重新摘要增加一次模型边界调用。
# 函数用途: 核对已锁存回合增长时取消仍立即终止。
def test_round_two_cancel_before_growth_unlock_never_retries_summary(monkeypatch):
    agent, params, calls, _ = arrange(monkeypatch, ProviderTransientError("temporary"), tokens=800)
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    params.cancellation_token.cancel("stop")
    monkeypatch.setattr(context_pressure, "model_visible_context_snapshot",
                        lambda *a, **k: SimpleNamespace(raw_estimated_tokens=920))
    with pytest.raises(InterruptedError):
        service._fit_native_ir_to_shared_budget(agent, params, "system")
    assert calls == [False]


# LLM: local_trim只处理受保护尾部以外的旧大结果；同输入重复进入不得继续换桩而改变逻辑调用身份。
# 函数用途: 验证70%以上确有局部缩减，原IR/用户/摘要/尾四对不变，重复续轮保持投影幂等。
def test_round_two_level_one_trims_old_output_without_mutating_source(monkeypatch):
    agent, params, calls, snapshot = arrange(monkeypatch, ProviderTransientError("temporary"), tokens=800)
    before = deepcopy(params.tool_ir_history)
    assert service._fit_native_ir_to_shared_budget(agent, params, "system") > 0
    assert params.tool_ir_history == before
    _, projected = project_native_prompt_history(params, "system")
    assert projected[:2] == before[:2]
    assert projected[-8:] == before[-8:]
    assert projected != before
    assert snapshot(agent, params, "system").raw_estimated_tokens < 800
    assert params.compact_degraded_baseline_tokens == 800
    assert service._fit_native_ir_to_shared_budget(agent, params, "system") == 0
    assert project_native_prompt_history(params, "system")[1] == projected
    assert params.compact_degraded_baseline_tokens == 800
    assert calls == [False]


# LLM: 校验值必须来自原始UTF-8正文，不按Unicode字符数或桩正文算哈希。
# 函数用途: 以1000个汉字核对原文字节数、摘要及可回查marker，保持原引用与原件。
def test_round_two_stub_contains_verifiable_utf8_digest_and_marker(monkeypatch):
    import hashlib

    agent, params, _, _ = arrange(monkeypatch, ProviderTransientError("temporary"), tokens=980)
    raw = replace(params.tool_ir_history[3], content_blocks=(ToolContentBlock("text", text="汉" * 1000),))
    params.tool_ir_history[3] = raw
    before = deepcopy(params.tool_ir_history)
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    _, projected = project_native_prompt_history(params, "system")
    data = json.loads(projected[3].output)
    payload = raw.output.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    assert data.get("bytes") == 3000 == len(payload)
    assert data["sha256"] == digest
    assert len(data["sha256"]) == 64
    assert data["marker"] == f"[TOOL_OUTPUT_COMPACTED: tool_call_id={raw.call_id} bytes=3000 sha256={digest}]"
    assert data["compact_authority"] == data["commit_authority"] == "turn_local"
    assert data["archive_ref"] == raw.refs[0].ref
    assert params.tool_ir_history == before


# LLM: 出站内容变更须产生新逻辑身份；同一投影重试稳定，清除覆盖恢复原指纹，不读写账本。
# 函数用途: 用实际发送消息独立重算指纹，验证临时桩参与指纹和logical_model_call_id。
def test_round_two_fingerprint_and_logical_identity_follow_actual_degraded_messages(monkeypatch):
    import hashlib

    from agent_py_agent.agent.agent_core.model.call_runtime import logical_model_call_id
    from agent_py_agent.agent.agent_core.tool_ir_history import provider_messages_fingerprint
    from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages

    agent, params, _, _ = arrange(monkeypatch, ProviderTransientError("temporary"), tokens=980)
    original = deepcopy(params.tool_ir_history)
    request = SimpleNamespace(agent=agent, params=params, prompt="system", tool_rounds=1)
    before_fingerprint = provider_messages_fingerprint(params)
    before_identity = logical_model_call_id(request)
    service._fit_native_ir_to_shared_budget(agent, params, "system")
    fingerprint = provider_messages_fingerprint(params)
    assert fingerprint != before_fingerprint
    assert logical_model_call_id(request) != before_identity
    outbound = _native_provider_messages(agent, params)
    expected = hashlib.sha256(json.dumps(outbound, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
    assert fingerprint == expected == provider_messages_fingerprint(params)
    identity = logical_model_call_id(request)
    assert logical_model_call_id(request) == identity
    assert params.tool_ir_history == original
    params.compact_degradation_stubs.clear()
    assert provider_messages_fingerprint(params) == before_fingerprint
    assert logical_model_call_id(request) == before_identity


# LLM: 字符变短不等于完整请求token变少；强制恢复必须拒绝未产生实际token收益的候选。
# 函数用途: 保留原可发送视图，避免换成更贵的桩后还把它当作新缩减重发。
@pytest.mark.parametrize("extra_tokens", [0, 20])
def test_round_two_force_rejects_stub_without_actual_token_reduction(monkeypatch, extra_tokens):
    agent, params, _, _ = arrange(monkeypatch, ProviderTransientError("temporary"), tokens=800)
    monkeypatch.setattr(context_pressure, "model_visible_context_snapshot", lambda a, p, prompt:
                        SimpleNamespace(raw_estimated_tokens=800 + (extra_tokens if p.compact_degradation_stubs else 0)))
    before = deepcopy(params.tool_ir_history)
    with pytest.raises(ConversationCompactError) as caught:
        service._fit_native_ir_to_shared_budget(agent, params, "system", force=True)
    assert caught.value.code == "COMPACT_BUDGET_EXCEEDED"
    assert params.tool_ir_history == before
    assert not params.compact_degradation_stubs
    assert not params.compact_degradation_latched


# LLM: 完整宿主请求复用同一局部裁剪与计量；无prompt的既有安全回退仍须携带全部观测别名和基线。
# 函数用途: 对照native/宿主的两层告警、来源权威与实际失败压力，不以日志文字做控制。
@pytest.mark.parametrize("entry,tokens,level", [
    ("native", 800, 1), ("native", 980, 2), ("host", 800, 1), ("host", 980, 2),
    ("host-known-no-prompt", 650, 1),
])
def test_round_two_warning_aliases_and_baseline_match_all_recovery_entries(monkeypatch, caplog, entry, tokens, level):
    from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
    from agent_py_agent.agent.agent_core.tool_request_capture import capture_tool_loop_request
    from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestInput
    from agent_py_agent.agent.conversation.models import ConversationHistorySeed
    from agent_py_agent.agent.prompting_parts.builder import PromptRenderInput

    error = ConversationCompactError("summary", code="COMPACT_FAILED")
    agent, params, _, _ = arrange(monkeypatch, error, tokens=tokens)
    with caplog.at_level(logging.WARNING):
        if entry == "native":
            service._fit_native_ir_to_shared_budget(agent, params, "system")
        else:
            params = replace(params, conversation_history_seed=ConversationHistorySeed())
            prompt = PromptRenderInput("system", "", "", "", "", (), "", "", "", "user", "", True)
            frozen = (ToolLoopRequestInput() if entry == "host-known-no-prompt"
                      else capture_tool_loop_request(agent, params, prompt))
            host = SimpleNamespace(force=False, agent=agent, calibration=None,
                                   source=SimpleNamespace(policy=SimpleNamespace(context_window_tokens=1000)),
                                   resolved_input=None, committed=False)
            monkeypatch.setattr(recovery, "_native_provider_history_messages", lambda *a, **k: [])
            assert recovery._restore_optional_preflight(host, params, frozen, tokens, error)
            assert not host.committed
    record = next(r for r in caplog.records if getattr(r, "compact_degraded_level", None) == level)
    expected = {"compact_degraded": True,
                "compact_fallback_level": "local_trim" if level == 1 else "stub_collapse",
                "compact_degraded_level": level, "compact_authority": "turn_local",
                "commit_authority": "turn_local", "original_reason_code": "COMPACT_FAILED", "reason": "COMPACT_FAILED"}
    for name, value in expected.items():
        assert getattr(record, name, None) == value, name
    assert params.compact_degraded_baseline_tokens == tokens
    if tokens > 700:
        assert params.compact_degradation_stubs


# LLM: 仅明确PARSE_FAILED分支可携带JSON解码原因；同一叶的存储/CAS硬码仍优先。
# 函数用途: 复现独审提出的解析原因链误拒，并核对cause/context/异常组不洗白硬停。
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("hard_code", [None, "STORE_CORRUPT", "CAS_CONFLICT"])
def test_round_two_tagged_summary_json_parse_chain_keeps_hard_precedence(monkeypatch, link, grouped, hard_code):
    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    decode = json.JSONDecodeError("fixture", "{", 1)
    if hard_code:
        decode.reason_code = hard_code
    error = ConversationCompactError("summary parse", code="COMPACT_PARSE_FAILED")
    setattr(error, link, builtins.ExceptionGroup("decode", [decode]) if grouped else decode)
    allowed = hard_code is None
    assert compact_failure_is_degradable(error)[0] is allowed
    agent, params, calls, _ = arrange(monkeypatch, error, tokens=650)
    original = deepcopy(params.tool_ir_history)
    if allowed:
        assert service._fit_native_ir_to_shared_budget(agent, params, "system") == 0
        assert params.compact_degradation_latched
    else:
        with pytest.raises(ConversationCompactError) as caught:
            service._fit_native_ir_to_shared_budget(agent, params, "system")
        assert caught.value is error
        assert not params.compact_degradation_latched
    assert calls == [False]
    assert params.tool_ir_history == original


# LLM: 没有显式摘要解析来源，JSONDecodeError或普通ValueError仍可能代表Store/IR损坏，不能猜安全。
# 函数用途: 守住新解析分支的窄边界，不让FAILED包装任意结构错误。
@pytest.mark.parametrize("code,cause", [("COMPACT_FAILED", json.JSONDecodeError("fixture", "{", 1)),
                                       ("COMPACT_PARSE_FAILED", ValueError("unknown structure"))])
def test_round_two_unknown_decode_or_structural_value_error_still_stops(code, cause):
    from agent_py_agent.agent.conversation.compact_guard import compact_failure_is_degradable

    error = ConversationCompactError("wrapper", code=code)
    error.__cause__ = cause
    assert compact_failure_is_degradable(error)[0] is False


# LLM: 真实调用先固化非空layout，再计算账本身份；指纹计算本身不得消费guidance或改原IR/prior。
# 函数用途: 独立核对动态宿主事实、既有消息和未转发指引下的出站投影指纹及重试稳定性。
def test_round_two_dynamic_prompt_prior_and_guidance_fingerprint_is_exact_and_readonly(monkeypatch):
    import hashlib

    from agent_py_agent.agent.agent_core.model.call_runtime import logical_model_call_id
    from agent_py_agent.agent.agent_core.tool_ir_history import provider_messages_fingerprint
    from agent_py_agent.agent.agent_core.tool_model_generation import (
        ModelGenerateParams,
        _materialize_native_prompt_facts,
        _native_provider_messages,
    )
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    agent, params, _, _ = arrange(monkeypatch, ProviderTransientError("temporary"), tokens=980)
    params.provider_history_messages.extend([{"role": "user", "content": "prior question"},
                                            {"role": "assistant", "content": "prior answer"}])
    params.tool_context.extend(["old guidance", "fresh guidance"])
    params.live_archive_state["_forwarded_runtime_guidance"] = {"old guidance"}
    prompt = CacheStructuredPrompt("stable system", canonical_user_turn=params.user_prompt,
                                   volatile_sections=(("round2.dynamic", "changing host fact"),))
    service._fit_native_ir_to_shared_budget(agent, params, prompt)
    request = _materialize_native_prompt_facts(ModelGenerateParams(agent, params, prompt, 0))
    before = deepcopy((params.tool_ir_history, params.provider_history_messages, params.live_archive_state))
    fingerprint = provider_messages_fingerprint(params)
    identity = logical_model_call_id(request)
    assert provider_messages_fingerprint(params) == fingerprint
    assert logical_model_call_id(request) == identity
    assert (params.tool_ir_history, params.provider_history_messages, params.live_archive_state) == before
    outbound = _native_provider_messages(agent, params)
    serialized = json.dumps(outbound, sort_keys=True, ensure_ascii=False, default=str)
    assert "changing host fact" in serialized and "fresh guidance" in serialized and "prior answer" in serialized
    assert "TOOL_OUTPUT_COMPACTED" in serialized
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16] == fingerprint
    assert provider_messages_fingerprint(params) == fingerprint
    assert logical_model_call_id(request) == identity
