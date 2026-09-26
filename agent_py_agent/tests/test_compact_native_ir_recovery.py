"""冻结真实原生工具 IR 的恢复安全点：只替换 HTTP，不伪造完整外层续跑。"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from functools import partial

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery as recovery_core
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
)
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolResult
from agent_py_agent.agent.conversation import active_turn_compact, background_execution, compact
from agent_py_agent.agent.conversation.background_compact_context import (
    apply_background_compact_context,
)
from agent_py_agent.agent.conversation.background_context import (
    prepare_background_context,
    render_background_context,
)
from agent_py_agent.agent.conversation.background_history_seed import (
    prepare_background_history_or_raise,
)
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.runtime import _run_params
from agent_py_agent.tests._tool_runtime_harness import canonical_history_call
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_mixed_compact_recovery import _wire_from_material
from agent_py_agent.tests.test_subagent_compact_recovery import _http


# LLM: 原后台完整准备仍只执行一次；read_file 经原执行器与 recorder 生成工具对，随后恢复宿主在同次冻结输入上裁决。
# pairs>1 时每对读取独立文件，使用独立轮次与 call/turn 身份；pairs=1 的文件名、轮次与身份保持原样。
# context_window_tokens 必须在历史准备前生效，使恢复来源策略与运行中策略读取同一窗口。
# 函数用途: 为纯 IR、空 transcript 的恢复安全点安装真实文件读取记录，并保留原 HTTP builder 与 CAS。
def _native_ir_attempt(
    tmp_path, monkeypatch, *, backend: str, full_result: str, archive_present: bool = True, pairs: int = 1,
    context_window_tokens: int | None = None,
):
    agent, store, thread, request, _, sink = _background(
        tmp_path, backend=backend, detached=True, with_history=False,
    )
    if context_window_tokens is not None:
        agent.config.model_context_window_tokens = context_window_tokens
        agent.backend.context_window_tokens = context_window_tokens
    request = replace(request, reason="audit_finding")
    agent.config.enable_tools = True
    agent.config.tool_output_preview_chars = 64
    agent.config.tool_output_externalize_min_chars = 10_000_000
    # 本夹具要故意造出"已经超预算的原生历史"来验证恢复宿主先压缩再发业务请求；默认开的余量外置会在归档时
    # 把第二页起的 read_file 结果外置并直接触发预检压缩，所以这里显式关掉它（余量外置有自己的回归）。
    agent.config.tool_output_externalize_on_low_headroom = False
    source_paths = [
        agent.home_paths.owner_workspace_dir / ("source.txt" if index == 0 else f"source-{index}.txt")
        for index in range(pairs)
    ]
    source_paths[0].parent.mkdir(parents=True, exist_ok=True)
    for source_path in source_paths:
        source_path.write_text(full_result, encoding="utf-8")
    execution = background_execution.BackgroundExecutionDependencies(
        agent, store, partial(_run_params, request=request, agent=agent),
    )
    history = prepare_background_history_or_raise(agent, store, thread, request)
    assert history.seed is None and not history.compact_source.messages
    assert history.compact_context.scope.kind == "turn"
    recorded = []
    original_loop = loop_support._tool_loop_execute_params

    def freeze_ir(*args, **kwargs):
        params = original_loop(*args, **kwargs)
        assert not params.archive_tool_calls
        from agent_py_agent.agent.agent_core._tool_loop_service import (
            _record_tool_call,
            execute_one_tool_call,
        )

        for index, source_path in enumerate(source_paths):
            suffix = f"-{index}" if index else ""
            call = canonical_history_call(
                "read_file", {"path": str(source_path)},
                call_id=f"native-source-call{suffix}", run_id=params.run_id,
                attempt_id=params.attempt_id, turn_id=f"{params.run_id}:source-turn{suffix}",
            )
            runtime = params.tool_runtime_snapshot.runtime("read_file")
            assert runtime is not None
            call = replace(call, schema_hash=runtime.model_spec.schema_hash)
            executed = execute_one_tool_call(agent, ToolCallExecuteParams(params, 1 + index, 0, call))
            assert executed.result.ok and full_result in executed.result.output

            _record_tool_call(agent, ToolCallRecordParams(
                params=params, tool_rounds=1 + index, idx=0, call=executed.call,
                result=executed.result, execution_states=executed.states, model_call=call,
            ))
            assert isinstance(params.tool_ir_history[-2], AssistantTurn)
            assert isinstance(params.tool_ir_history[-1], ToolResult)
            recorded.append((params.tool_ir_history[-2:], params.archive_tool_calls[-1]))
        if not archive_present:
            params.archive_tool_calls.clear()
        return params

    monkeypatch.setattr(loop_support, "_tool_loop_execute_params", freeze_ir)
    params = execution.prepare_run(thread.thread_id, thread=thread, history_seed=history.seed)
    params.compact_context = history.compact_context
    context = prepare_background_context(
        agent=agent, store=store, thread=thread, request=request,
        proactive_delivery_available=False, include_recent_messages=False,
        context_bundle=history.context_bundle,
    )
    context = apply_background_compact_context(context, history.compact_context, thread.compact_generation)
    params.inject = [render_background_context(context)]
    params.on_chunk = sink
    sink.begin_model_attempt(1)
    return agent, store, thread, execution, sink, history, params, context, recorded


@pytest.mark.parametrize("backend,archive_present", [
    ("anthropic_compatible", True),
    ("openai_compatible", True),
    ("anthropic_compatible", False),
])
def test_empty_transcript_native_ir_recovery_summarizes_full_result_once_and_sends_candidate(
    tmp_path, monkeypatch, backend, archive_present,
) -> None:
    marker = "EXACT_FULL_TOOL_BODY_" + "x" * 5_000
    agent, store, thread, execution, sink, history, params, context, recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend=backend, full_result=marker, archive_present=archive_present,
    )
    candidates, summaries, business, source_plans = [], [], [], []
    cas_calls = []
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_cas = store.threads.update_compact_state
    in_summary = False

    def commit(*args, **kwargs):
        cas_calls.append((args, kwargs))
        return original_cas(*args, **kwargs)

    def project(*args):
        material = original_project(*args)
        if args[1].is_candidate:
            candidates.append(material)
        return material

    def summary(*args, **kwargs):
        nonlocal in_summary
        source_plans.append(args[1])
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    monkeypatch.setattr(store.threads, "update_compact_state", commit)
    measured = []
    original_measure = recovery_core._full_request_tokens
    monkeypatch.setattr(recovery_core, "_full_request_tokens",
                        lambda *args: measured.append(original_measure(*args)) or measured[-1])

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
        else:
            business.append(wire)
            assert len(candidates) == 1 and wire == _wire_from_material(agent, candidates[0])

    all_wires, _ = _http(monkeypatch, backend=backend, on_business=on_http)
    result, _, recovery = background_execution._run_background_recovery_attempt(
        execution, "继续核对完整工具结果", params, history, context,
        recovering=True, activity_sink=sink,
    )

    assert result.runtime_status != "context_overflow"
    assert recovery is not None and recovery.committed
    assert len(cas_calls) == 1
    assert len(recorded) == len(source_plans) == len(candidates) == len(summaries) == len(business) == 1
    assert len(all_wires) == 2
    native_ir, archived = recorded[0]
    assert archived["call_id"] == native_ir[0].tool_calls[0].call_id
    assert len(archived["output_preview"]) < len(native_ir[1].output)
    assert marker in native_ir[1].output
    assert len(source_plans[0].source_records) == int(archive_present)
    assert source_plans[0].source_ir_history == tuple(native_ir)
    assert len(source_plans[0].source_tool_refs) == 1
    summary_messages = json.dumps(summaries[0].get("messages"), ensure_ascii=False)
    assert marker in summary_messages
    assert summary_messages.count("EXACT_FULL_TOOL_BODY_") == 1
    candidate = candidates[0]
    assert not any(isinstance(item, (AssistantTurn, ToolResult)) for item in candidate.request_input.tool_ir_history)
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == 1 and committed.compact_checkpoint_id
    # “压缩前”取 select 解绑旧历史前量得的完整旧请求，活动回合路径不再用已解绑的冻结输入重量。
    assert len(measured) == 1
    assert committed_compact_checkpoint_chain(agent, committed)[-1]["projected_tokens_before"] == measured[0]
    view = resolve_compact_summary_view(agent, committed, history.compact_context.scope)
    assert view.source_tool_refs == tuple(source_plans[0].source_tool_refs)
    assert (archived in candidate.params.archive_tool_calls) is archive_present


# LLM: 满足生产六字段交接合同的摘要正文，只作摘要回复替身；不能作为执行事实或验收结论。
# 函数用途: 让恢复摘要走真实模型摘要分支而非机械兜底，候选大小由真实摘要决定。
def _live_handoff_text() -> str:
    return (
        "[compact-live-handoff.v1]\n"
        "current_progress: 已读取全部原始资料文件，并完成第一轮逐项核对，核对记录保存在工具账中。\n"
        "user_constraints: 保留原始文件与已有记录，不改写任何资料，只输出核对结论。\n"
        "completed: 多个原始资料文件均已通过 read_file 完整读取，读取结果已记录。\n"
        "failures: 本轮没有工具失败。\n"
        "unresolved: 还需要根据已读取的资料给出最终核对结论。\n"
        "next_step: 直接依据本摘要继续核对，不再重复读取同一批文件。"
    )


# 第8步行为变化的回归：恢复宿主领取本次 Compact 时，build 跳过共享预算回收，
# 由宿主在发送前用同次完整计量把超预算原生历史收进窗口。
@pytest.mark.parametrize("recovering", [False, True])
def test_owned_recovery_fits_over_budget_native_history_before_business_request(
    tmp_path, monkeypatch, recovering,
) -> None:
    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        projected_model_context_components,
    )
    from agent_py_agent.agent.agent_core.tool_request_projection import project_tool_loop_request
    from agent_py_agent.agent.backends import http
    from agent_py_agent.agent.model_request_selection import request_owns_compact

    body = "OVER_BUDGET_TOOL_BODY_" + "资料核对" * 3_700
    agent, store, thread, execution, sink, history, params, context, recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result=body, pairs=4,
        context_window_tokens=60_000,
    )
    ceiling = compact._compact_request_input_ceiling(agent, history.compact_source.policy)
    frozen_requests, shared_fits, candidates, summaries, business = [], [], [], [], []
    original_capture = recovery_core.capture_tool_loop_request
    original_fit = service._fit_native_ir_to_shared_budget
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    in_summary = False

    def capture(agent_, params_, prompt_input):
        frozen = original_capture(agent_, params_, prompt_input)
        tokens, _ = projected_model_context_components(project_tool_loop_request(frozen))
        policy = service._native_compact_policy(agent_, params_)
        frozen_requests.append((tokens, policy.trigger_tokens, len(service._native_tool_call_ids(params_))))
        return frozen

    def fit(agent_, params_, prompt, **kwargs):
        shared_fits.append(request_owns_compact(agent_, params_))
        return original_fit(agent_, params_, prompt, **kwargs)

    def project(*args):
        material = original_project(*args)
        if args[1].is_candidate:
            candidates.append(material)
        return material

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        names = [row.get("name") for row in wire.get("tools", [])]
        if names == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            probe = {"type": "tool_use", "id": "probe", "name": names[0], "input": {"nonce": nonce}}
            return {"content": [probe], "stop_reason": "tool_use"}
        if in_summary:
            summaries.append(wire)
            text = _live_handoff_text()
        else:
            business.append(wire)
            # 发送前：共享预算回收没有介入；宿主已提交候选，并用同次完整计量证明它低于输入上界。
            assert shared_fits == []
            assert len(candidates) == 1 and wire == _wire_from_material(agent, candidates[0])
            assert projected_model_context_components(candidates[0].projection)[0] < ceiling
            assert "OVER_BUDGET_TOOL_BODY_" not in json.dumps(wire, ensure_ascii=False)
            text = "资料核对完成。"
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}

    monkeypatch.setattr(recovery_core, "capture_tool_loop_request", capture)
    monkeypatch.setattr(service, "_fit_native_ir_to_shared_budget", fit)
    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    monkeypatch.setattr(http, "post_json", send)
    result, _, recovery = background_execution._run_background_recovery_attempt(
        execution, "继续核对完整工具结果", params, history, context,
        recovering=recovering, activity_sink=sink,
    )

    # 对照：同次冻结的原始完整请求确实超过共享预算，且包含多对可回收的原生工具往返。
    raw_tokens, shared_trigger, native_pairs = frozen_requests[0]
    assert raw_tokens >= shared_trigger > ceiling - 1
    assert native_pairs == len(recorded) == 4
    assert result.runtime_status != "context_overflow"
    assert recovery is not None and recovery.committed
    assert summaries and len(business) == 1
    assert store.threads.require(thread.thread_id).compact_generation == 1


@pytest.mark.parametrize("failure", ["cancel", "too_large"])
def test_native_ir_recovery_failure_sends_no_business_or_commit(tmp_path, monkeypatch, failure) -> None:
    agent, store, thread, execution, sink, history, params, context, recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result="FULL-CONTENT-" + "y" * 5_000,
    )
    summaries, business = [], []
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_ceiling = compact._compact_request_input_ceiling
    original_cas = store.threads.update_compact_state
    cas_calls = []
    in_summary = False

    def commit(*args, **kwargs):
        cas_calls.append((args, kwargs))
        return original_cas(*args, **kwargs)

    def project(*args):
        if failure == "cancel" and args[1].is_candidate:
            raise InterruptedError("测试取消原生IR候选")
        return original_project(*args)

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    monkeypatch.setattr(store.threads, "update_compact_state", commit)
    if failure == "too_large":
        monkeypatch.setattr(
            compact, "_compact_request_input_ceiling",
            lambda *args: 1 if summaries else original_ceiling(*args),
        )

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
        else:
            business.append(wire)
            pytest.fail("原生 IR 候选未提交时发送了业务请求")

    _http(monkeypatch, backend="anthropic_compatible", on_business=on_http)
    expected_error = InterruptedError if failure == "cancel" else ConversationCompactError
    with pytest.raises(expected_error) as raised:
        background_execution._run_background_recovery_attempt(
            execution, "继续核对完整工具结果", params, history, context,
            recovering=True, activity_sink=sink,
        )
    assert len(recorded) == len(summaries) == 1
    assert not business
    assert not cas_calls
    if failure == "too_large":
        assert raised.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == 0 and not committed.compact_checkpoint_id


def test_overflow_capture_release_error_preserves_completed_native_turn(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import guidance

    marker = "COMPLETED_BEFORE_RELEASE_ERROR"
    agent, store, thread, execution, sink, history, params, context, recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result=marker,
    )
    persisted = []
    params.partial_turn_callback = persisted.append

    def release_failure(_agent, actual_params):
        assert any(isinstance(item, ToolResult) and marker in item.output
                   for item in actual_params.tool_ir_history)
        raise OSError("测试插话账本释放失败")

    def overflow(_wire, _number):
        raise ProviderContextWindowError("测试供应商超窗")

    monkeypatch.setattr(guidance, "release_reserved_turn_input_after_attempt", release_failure)
    _http(monkeypatch, backend="anthropic_compatible", on_business=overflow)
    with pytest.raises(OSError, match="插话账本释放失败"):
        background_execution._run_background_recovery_attempt(
            execution, "继续核对", params, history, context, recovering=False, activity_sink=sink,
        )
    assert len(recorded) == len(persisted) == 1
    assert marker in json.dumps(persisted[0].canonical_native_messages, ensure_ascii=False)
    assert store.threads.require(thread.thread_id).compact_generation == 0


# 原场景（发现 1）：部署前写入的工具索引行没有 attempt/turn 身份。当前轮只剩这类旧记录、又没有已结束的
# transcript 时，强制恢复不能把原因报成"没有来源"，要报结构化的来源身份不可证明，且不发业务请求、不提交。
def test_forced_recovery_with_identityless_carried_records_reports_coverage_unknown(tmp_path, monkeypatch) -> None:
    agent, store, thread, execution, sink, history, params, context, recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result="LEGACY-ROW-" + "z" * 2_000,
    )
    recorded_loop = loop_support._tool_loop_execute_params

    # 模拟重启后从旧耐久索引恢复的记录：只剩归档行且缺 attempt/turn，当前参数里没有对应的原生往返。
    def legacy_rows(*args, **kwargs):
        loop_params = recorded_loop(*args, **kwargs)
        assert loop_params.archive_tool_calls
        for record in loop_params.archive_tool_calls:
            record.pop("attempt_id", None)
            record.pop("turn_id", None)
        loop_params.tool_ir_history[:] = [
            item for item in loop_params.tool_ir_history if not isinstance(item, (AssistantTurn, ToolResult))
        ]
        return loop_params

    monkeypatch.setattr(loop_support, "_tool_loop_execute_params", legacy_rows)
    business, cas_calls = [], []
    original_cas = store.threads.update_compact_state

    def commit(*args, **kwargs):
        cas_calls.append((args, kwargs))
        return original_cas(*args, **kwargs)

    monkeypatch.setattr(store.threads, "update_compact_state", commit)
    _http(monkeypatch, backend="anthropic_compatible", on_business=lambda wire, _number: business.append(wire))
    with pytest.raises(ConversationCompactError) as raised:
        background_execution._run_background_recovery_attempt(
            execution, "继续核对完整工具结果", params, history, context,
            recovering=True, activity_sink=sink,
        )
    assert raised.value.code == "COMPACT_TOOL_COVERAGE_UNKNOWN"
    assert not business and not cas_calls
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == 0 and not committed.compact_checkpoint_id


# LLM: 后台宿主直接安装 PreparedCompactRecovery：首请求超预算，由宿主自动恢复并提交候选；业务响应按发送序号由
# respond 脚本决定（可抛错），摘要走真实模型摘要分支。只记录事实，不改变被测流程。
# 函数用途: 跑一次"提交候选后发送"的后台尝试，记录每次业务发送、发送前的重建与共享回收次数、回收是否落在原参数上、已提交候选。
def _committed_background_attempt(tmp_path, monkeypatch, respond):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume
    from agent_py_agent.agent.backends import http
    from agent_py_agent.agent.model_request_selection import _HOST

    body = "OVER_BUDGET_TOOL_BODY_" + "资料核对" * 3_700
    agent, store, thread, execution, sink, history, params, context, _recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result=body, pairs=4,
        context_window_tokens=60_000,
    )
    run = SimpleNamespace(agent=agent, store=store, thread=thread, candidates=[], summaries=[], business=[],
                          builds=[], stale_fits=[], committed=[], waits=[], result=None, recovery=None)
    original_fit = service._fit_native_ir_to_shared_budget
    original_build = service.build_tool_loop_prompt
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_select = recovery_core.PreparedCompactRecovery.select
    in_summary = False

    def fit(agent_, params_, *args, **kwargs):
        run.stale_fits.append(params_ is getattr(_HOST.get(), "render_params", None))
        return original_fit(agent_, params_, *args, **kwargs)

    def build(*args, **kwargs):
        run.builds.append(True)
        return original_build(*args, **kwargs)

    def project(*args):
        material = original_project(*args)
        if args[1].is_candidate:
            run.candidates.append(material)
        return material

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    def select(self, *args):
        value = original_select(self, *args)
        if self.committed and not run.committed:
            run.committed.append(value[0])
        return value

    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        names = [row.get("name") for row in wire.get("tools", [])]
        if names == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            probe = {"type": "tool_use", "id": "probe", "name": names[0], "input": {"nonce": nonce}}
            return {"content": [probe], "stop_reason": "tool_use"}
        if in_summary:
            run.summaries.append(wire)
            return {"content": [{"type": "text", "text": _live_handoff_text()}], "stop_reason": "end_turn"}
        run.business.append((wire, len(run.builds), len(run.stale_fits)))
        return respond(run, len(run.business))

    monkeypatch.setattr(service, "_fit_native_ir_to_shared_budget", fit)
    monkeypatch.setattr(service, "build_tool_loop_prompt", build)
    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    monkeypatch.setattr(recovery_core.PreparedCompactRecovery, "select", select)
    monkeypatch.setattr(provider_transient_auto_resume, "_wait_before_retry", lambda *_: run.waits.append(True))
    monkeypatch.setattr(http, "post_json", send)
    run.result, _, run.recovery = background_execution._run_background_recovery_attempt(
        execution, "继续核对完整工具结果", params, history, context, recovering=False, activity_sink=sink,
    )
    assert run.recovery is not None and run.recovery.committed and len(run.candidates) == 1 and run.summaries
    assert store.threads.require(thread.thread_id).compact_generation == 1
    return run


_DONE = {"content": [{"type": "text", "text": "资料核对完成。"}], "stop_reason": "end_turn"}
_EMPTY = {"content": [], "stop_reason": "end_turn"}


# 提交候选后的瞬断重试沿同一原参数命中已提交记录：原样重发同一候选，不在原参数上重建或做共享预算回收。
def test_background_transient_retry_after_commit_resends_committed_candidate(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.backends.errors import ProviderTransientError

    def respond(_run, index):
        if index == 1:
            raise ProviderTransientError("503 after commit")
        return _DONE

    run = _committed_background_attempt(tmp_path, monkeypatch, respond)
    expected = _wire_from_material(run.agent, run.candidates[0])
    assert len(run.waits) == 1 and [wire == expected for wire, _, _ in run.business] == [True, True]
    assert run.business[0][1:] == run.business[1][1:], "重试没有重建，也没有共享预算回收"
    assert run.stale_fits == []
    assert run.result.runtime_status != "context_overflow"


# 候选返回空响应：同轮空响应修复沿候选参数重建并附修复提示；原参数上不发生共享预算回收（否则抛 compact summary base changed）。
def test_background_empty_response_after_commit_repairs_on_candidate_params(tmp_path, monkeypatch) -> None:
    run = _committed_background_attempt(tmp_path, monkeypatch, lambda _run, index: _EMPTY if index == 1 else _DONE)
    expected = _wire_from_material(run.agent, run.candidates[0])
    assert len(run.business) == 2
    (first, *_), (repaired, *_) = run.business
    hint = "上一轮模型接口返回了空文本"
    assert first == expected and hint not in json.dumps(first, ensure_ascii=False)
    assert repaired != expected and json.dumps(repaired, ensure_ascii=False).count(hint) == 1
    assert run.stale_fits and not any(run.stale_fits), "共享预算回收只落在候选参数上"
    assert run.result.runtime_status == "ok"


# 候选在途时到达插话且候选返回空响应：插话取代分支把插话注入候选参数，只注入一次；重跑请求带这条插话，确认后邮箱清空。
def test_background_steer_after_commit_injects_once_on_candidate_params(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.model_request_selection import _HOST

    message = "补充要求：核对结论按文件名排序。"
    targets = []

    def respond(run, index):
        if index == 1:
            request_id = _HOST.get().render_params.request_id
            targets.append(request_id)
            run.store.guidance.append({"target_type": "request", "target_id": request_id, "message": message, "now": 10.0})
            return _EMPTY
        return _DONE

    run = _committed_background_attempt(tmp_path, monkeypatch, respond)
    expected = _wire_from_material(run.agent, run.candidates[0])
    assert len(run.business) == 2, "空响应后只重跑一次，重跑即带插话"
    (first, *_), (rerun, *_) = run.business
    assert first == expected and message not in json.dumps(first, ensure_ascii=False)
    assert json.dumps(rerun, ensure_ascii=False).count(message) == 1
    committed = run.committed[0]
    assert sum(message in str(item) for item in committed.tool_context) == 1, "插话只在候选参数上注入一次"
    assert run.store.guidance.pending("request", targets[0]) == []
    assert run.stale_fits and not any(run.stale_fits)
