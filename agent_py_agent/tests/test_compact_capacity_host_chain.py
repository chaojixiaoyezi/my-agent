"""“候选过大”容量计量走真实 PreparedCompactRecovery 两个入口（2026-09-28）。

背景：Codex 离线复现（compact-2214052）发现真实宿主里固定开销恒为 0：固定开销那一版空摘要撞上候选替换入口的
非空摘要合同，投影报 COMPACT_REQUEST_PROJECTION_UNKNOWN 后被记成 0；保留 IR 又按来源保留区计，把候选会整体替换的
旧摘要/旧交接也算了进去。
锁定：transcript 与活动回合两个入口都经宿主的“只计量、不提交”入口（measure_only）实测固定开销，只在真正抛出
COMPACT_CANDIDATE_TOO_LARGE 时量一次，成功路径不多投影；保留 IR 按最小候选实际要发送的材料计。
只替身摘要模型、末端 HTTP，以及在需要时把输入上限压到 1 迫使候选被拒；宿主准备、候选投影、计量和进度发射都走生产链。
"""
from __future__ import annotations

from dataclasses import asdict, replace
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import compact_active_projection as active_projection
from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
from agent_py_agent.agent.agent_core.compact_active_projection import (
    _replace_compact_history,
    replace_recovery_active_tools,
)
from agent_py_agent.agent.agent_core.model.context_pressure import (
    projected_model_context_components,
)
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import CompactionSummary, RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation import active_turn_compact, background_execution, compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_tool_summary import (
    compact_tool_summary_history,
    compact_tool_summary_text,
    retained_ir_facts,
    sent_retained_ir,
)
from agent_py_agent.agent.conversation.runtime import _run_params
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_compact_active_projection import _fixture
from agent_py_agent.tests.test_compact_native_ir_recovery import _native_ir_attempt
from agent_py_agent.tests.test_compact_output_reserve import _case
from agent_py_agent.tests.test_mixed_compact_recovery import _mixed_background
from agent_py_agent.tests.test_subagent_compact_recovery import _http

# 测试侧独立写出的“候选会整体替换”的两种载体，不引用产品常量，避免与被测口径同源。
_REPLACED_SOURCES = ("applied_compact", "carried_tool_handoff")


# LLM: 只装“先调原实现再记录”的旁听器，不替换任何投影、计量或进度逻辑；记录的是生产链真实交回的对象。
# 函数用途: 记下宿主候选投影、只计量投影和失败进度字段，供断言固定开销的实测次数与保留 IR 口径。
def _listen(monkeypatch):
    seen = SimpleNamespace(candidates=[], measured=[], failures=[], failed_in=[], callee=[])
    original_mixed = recovery._project_mixed_recovery_material
    original_measure = recovery._measure_only_projection
    original_callee = active_projection.replace_recovery_active_tools

    def mixed(material, view, max_chars):
        value = original_mixed(material, view, max_chars)
        if view.is_candidate:
            seen.candidates.append((view, value))
        return value

    def measure(material, view, max_chars):
        value = original_measure(material, view, max_chars)
        seen.measured.append((view, value))
        return value

    # 真实联合替换入口（mixed callee）：记下每次调用是不是只计量，以及是否成功交回。
    def callee(*args, **kwargs):
        value = original_callee(*args, **kwargs)
        seen.callee.append(bool(kwargs.get("measure_only")))
        return value

    def fields_of(module, original, exc):
        seen.failed_in.append(module.__name__.rsplit(".", 1)[-1])
        seen.failures.append(original(exc))
        return seen.failures[-1]

    monkeypatch.setattr(recovery, "_project_mixed_recovery_material", mixed)
    monkeypatch.setattr(recovery, "_measure_only_projection", measure)
    monkeypatch.setattr(active_projection, "replace_recovery_active_tools", callee)
    for module in (compact, active_turn_compact):
        monkeypatch.setattr(module, "compact_failure_progress_fields",
                            partial(fields_of, module, module.compact_failure_progress_fields))
    return seen


# 函数用途: 按宿主候选闭包的同一口径（完整投影 + 已知媒体预留）读出一个候选材料的 token 数。
def _tokens(material) -> int:
    return projected_model_context_components(material.projection, media_token_reserve=recovery.media_token_reserve())[0]


# 函数用途: 从候选实际要发送的冻结输入里取出不是摘要/交接载体的原生 IR（测试侧独立口径）。
def _sent_ir(material) -> tuple[object, ...]:
    return tuple(item for item in material.request_input.tool_ir_history if not (
        isinstance(item, CompactionSummary) and item.source in _REPLACED_SOURCES
    ))


# 函数用途: 取失败留下的容量计量：有进度通道的宿主读 failed 进度字段；测试夹具里 Gateway 没接进度通道，
#   就沿异常链读宿主包装前的原始失败计量。
def _failure_fields(seen, error) -> dict[str, object]:
    if seen.failures:
        fields, = seen.failures
        return fields
    while error is not None and getattr(error, "capacity", None) is None:
        error = error.__cause__
    assert error is not None, "失败没有留下容量计量"
    return {"error_code": error.code, **asdict(error.capacity)}


# 函数用途: 断言失败只量了一次固定开销，且量的是宿主“空摘要、无保留”的只计量版本，结果如实写进失败字段。
def _assert_fixed_measured_once(seen, fields) -> None:
    assert fields["error_code"] == "COMPACT_CANDIDATE_TOO_LARGE"
    (view, measured), = seen.measured
    assert view.measure_only and view.summary == "" and view.retained_tool_records == ()
    assert view.retained_ir_history == ()
    assert not isinstance(measured.material, recovery.CompactRecoveryMaterial)
    # 固定开销确实经真实联合替换入口投影成功，且整条链只有这一次只计量调用。
    assert seen.callee.count(True) == 1
    assert fields["fixed_tokens"] == measured.projected_tokens
    assert 0 < fields["fixed_tokens"] < fields["candidate_tokens"], fields
    # 候选替换入口从不拿空摘要投影：固定开销只走只计量入口。
    assert seen.candidates and all(view.summary and not view.measure_only for view, _ in seen.candidates)


# 三种宿主的 transcript 入口：输出预留让最小候选自然被拒（不压上限）。
@pytest.mark.parametrize("host", ["gateway", "child", "background"])
def test_transcript_entry_measures_fixed_overhead_once_through_the_host(tmp_path, monkeypatch, host):
    _agent, _tid, run, _requirement = _case(tmp_path, host, "anthropic_compatible", large=True)
    seen = _listen(monkeypatch)
    monkeypatch.setattr(compact, "_summarize", lambda *args, **kwargs: "旧资料已完成核对，后续遵守当前完整要求。")
    sent = []
    _http(monkeypatch, backend="anthropic_compatible", on_business=lambda wire, _number: sent.append(wire))
    error = None
    if host == "child":
        assert run().ok is False
    else:
        with pytest.raises((RuntimeError, ConversationCompactError)) as failure:
            run()
        error = failure.value
    assert sent == []
    # 夹具里 Gateway 没接进度通道，其余宿主的失败都出自 transcript 压缩链。
    assert seen.failed_in == ([] if host == "gateway" else ["compact"])
    fields = _failure_fields(seen, error)
    _assert_fixed_measured_once(seen, fields)
    smallest = min((material for _, material in seen.candidates), key=_tokens)
    assert _tokens(smallest) == fields["candidate_tokens"]
    assert (fields["retained_ir_items"], fields["retained_ir_tokens"]) == retained_ir_facts(_sent_ir(smallest))
    # 最小候选不留原话尾部：候选 ≈ 固定开销 + 摘要 + 保留 IR，只差摘要载体的包装（几百 token）。
    # 宿主把当前要求放在系统提示（gateway/background）还是当前任务 IR（child）只改变两项的分配，不改变合计。
    assert fields["retained_items"] == 0
    gap = fields["candidate_tokens"] - fields["fixed_tokens"] - fields["summary_tokens"] - fields["retained_ir_tokens"]
    assert 0 <= gap < 1_000, fields


# 联合来源（已结束历史 + 携带工具归档）：来源保留区带着旧工具交接，保留 IR 只能按候选实际发送的材料计。
@pytest.mark.parametrize("outcome", ["too_large", "committed"])
def test_mixed_transcript_entry_counts_only_ir_the_candidate_sends(tmp_path, monkeypatch, outcome):
    _agent, _store, thread, request, execution, sink, _ = _mixed_background(
        tmp_path, backend="anthropic_compatible", detached=False,
    )
    seen, sources, business = _listen(monkeypatch), [], []
    original_source = recovery._recovery_tool_source
    monkeypatch.setattr(recovery, "_recovery_tool_source",
                        lambda *args: sources.append(original_source(*args)) or sources[-1])
    monkeypatch.setattr(compact, "_summarize", lambda *args, **kwargs: "联合来源已核对，继续后续步骤。")
    if outcome == "too_large":
        original_ceiling = compact._compact_request_input_ceiling
        monkeypatch.setattr(compact, "_compact_request_input_ceiling",
                            lambda *args: 1 if business else original_ceiling(*args))

    def on_http(wire, _number):
        business.append(wire)
        if len(business) == 1:
            raise ProviderContextWindowError("测试联合来源溢出")

    _http(monkeypatch, backend="anthropic_compatible", on_business=on_http)
    run = partial(background_execution.run_background_turn_with_compact, execution, thread, request,
                  user_prompt="继续联合核对", continuation_injection=[], proactive_delivery_available=False,
                  activity_sink=sink)
    if outcome == "committed":
        assert run().runtime_status != "context_overflow" and len(business) == 2
        # 成功路径不做失败诊断用的固定开销实测。
        assert seen.candidates and seen.measured == [] and seen.failures == []
        return
    with pytest.raises(ConversationCompactError):
        run()
    assert len(business) == 1 and seen.failed_in == ["compact"]
    fields, = seen.failures
    _assert_fixed_measured_once(seen, fields)
    smallest = min((material for _, material in seen.candidates), key=_tokens)
    assert _tokens(smallest) == fields["candidate_tokens"]
    assert (fields["retained_ir_items"], fields["retained_ir_tokens"]) == retained_ir_facts(_sent_ir(smallest))
    # 来源保留区里的旧交接会被候选整体替换，不随候选发送；按来源保留区计会多算。
    source_ir = sources[-1].retained_ir_history
    assert any(isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff" for item in source_ir)
    assert retained_ir_facts(source_ir)[0] > fields["retained_ir_items"]


# 活动回合入口（空 transcript、原生工具 IR）：固定开销走宿主 fixed_request_projector，候选投影器不再收到空摘要。
@pytest.mark.parametrize("outcome", ["too_large", "committed"])
def test_active_entry_measures_fixed_overhead_through_the_host_only_on_failure(tmp_path, monkeypatch, outcome):
    _agent, _store, _thread, execution, sink, history, params, context, _recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result="FULL-CONTENT-" + "y" * 5_000,
    )
    seen, summaries, business = _listen(monkeypatch), [], []
    original_summary = active_turn_compact._active_turn_replacement_summary
    in_summary = False

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    if outcome == "too_large":
        original_ceiling = compact._compact_request_input_ceiling
        monkeypatch.setattr(compact, "_compact_request_input_ceiling",
                            lambda *args: 1 if summaries else original_ceiling(*args))
    _http(monkeypatch, backend="anthropic_compatible",
          on_business=lambda wire, _number: (summaries if in_summary else business).append(wire))
    run = partial(background_execution._run_background_recovery_attempt, execution, "继续核对完整工具结果",
                  params, history, context, recovering=True, activity_sink=sink)
    if outcome == "committed":
        _result, _resolved, prepared = run()
        assert prepared is not None and prepared.committed and len(business) == 1
        assert seen.candidates and seen.measured == [] and seen.failures == []
        return
    with pytest.raises(ConversationCompactError):
        run()
    assert business == [] and len(summaries) == 1 and seen.failed_in == ["active_turn_compact"]
    fields, = seen.failures
    _assert_fixed_measured_once(seen, fields)
    assert fields["candidates_tried"] == 1
    (_view, candidate), = seen.candidates
    assert _tokens(candidate) == fields["candidate_tokens"]
    assert (fields["retained_ir_items"], fields["retained_ir_tokens"]) == retained_ir_facts(_sent_ir(candidate))


# 函数用途: 空 transcript、只带携带工具归档的后台夹具：恢复走活动回合入口，来源保留区里带着旧工具交接。
def _carried_active_background(tmp_path):
    agent, store, thread, request, _, sink = _background(
        tmp_path, backend="anthropic_compatible", detached=False, with_history=False,
    )
    archived = [{
        "call_id": f"call-{number}", "scoped_call_id": f"run-carried:call-{number}", "run_id": "run-carried",
        "attempt_id": "attempt-carried", "turn_id": "turn-carried", "tool": "read_file", "ok": True,
        "model_parameters": {"path": f"source-{number}.txt"},
        "model_summary": f"CARRIED_SOURCE_{number} " + "完整工具内容" * 80, "output_preview": f"工具结果 {number}",
    } for number in range(2)]

    def prepare(thread_id, *, thread, history_seed):
        params = _run_params(thread_id, request, agent, thread=thread, history_seed=history_seed)
        params.carried_archive_tool_calls = list(archived)
        return params

    return thread, request, background_execution.BackgroundExecutionDependencies(agent, store, prepare), sink


# 活动回合入口的来源保留区带着旧交接时，保留 IR 仍只按候选实际发送的材料计，不能退回来源保留区口径。
def test_active_entry_with_carried_handoff_counts_only_ir_the_candidate_sends(tmp_path, monkeypatch):
    thread, request, execution, sink = _carried_active_background(tmp_path)
    seen, sources, business = _listen(monkeypatch), [], []
    original_source = recovery._recovery_tool_source
    monkeypatch.setattr(recovery, "_recovery_tool_source",
                        lambda *args: sources.append(original_source(*args)) or sources[-1])
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary",
                        lambda *args, **kwargs: "携带归档已核对，继续后续步骤。")
    original_ceiling = compact._compact_request_input_ceiling
    monkeypatch.setattr(compact, "_compact_request_input_ceiling",
                        lambda *args: 1 if business else original_ceiling(*args))

    def on_http(wire, _number):
        business.append(wire)
        raise ProviderContextWindowError("测试携带归档溢出")

    _http(monkeypatch, backend="anthropic_compatible", on_business=on_http)
    with pytest.raises(ConversationCompactError):
        background_execution.run_background_turn_with_compact(
            execution, thread, request, user_prompt="继续核对携带归档", continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink,
        )
    assert len(business) == 1 and seen.failed_in == ["active_turn_compact"]
    fields, = seen.failures
    _assert_fixed_measured_once(seen, fields)
    assert fields["candidates_tried"] == 1
    (_view, candidate), = seen.candidates
    assert _tokens(candidate) == fields["candidate_tokens"]
    assert (fields["retained_ir_items"], fields["retained_ir_tokens"]) == retained_ir_facts(_sent_ir(candidate))
    source_ir = sources[-1].retained_ir_history
    assert any(isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff" for item in source_ir)
    assert retained_ir_facts(source_ir)[0] > fields["retained_ir_items"]


# 计量口径与发送口径必须一致：候选投影原样保留（按对象身份）的 IR 正好是 sent_retained_ir 留下的条目，
# 被替换的正好是两种摘要/交接载体；投影后再取一次也只剩这些原条目。改任一边都要让这里同步。
@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("handoff", ["", "新的工具交接"])
def test_sent_retained_ir_matches_what_the_candidate_projection_keeps(seeded, handoff):
    history = [
        UserTurn("当前任务"),
        CompactionSummary("旧会话摘要", source="applied_compact"),
        CompactionSummary("旧工具交接", source="carried_tool_handoff"),
        RuntimeFactsTurn(text="运行事实", source="conversation.runtime"),
        CompactionSummary("没有来源的旧载体"),
        UserTurn("插话"),
    ]
    params = SimpleNamespace(conversation_history_seed=object() if seeded else None)
    context = SimpleNamespace(view=SimpleNamespace(summary="新摘要", generation=2))
    projected = _replace_compact_history(history, params, context, handoff)
    kept = [item for item in projected if any(item is original for original in history)]
    assert [id(item) for item in kept] == [id(item) for item in sent_retained_ir(history)]
    assert [id(item) for item in sent_retained_ir(projected)] == [id(item) for item in kept]
    assert len(kept) == 4


# Codex 复核（compact-author-patch-review-20260928T080805Z）的同一夹具：带种子时真实替换只换掉旧会话摘要与旧交接，
# 来源为空的 CompactionSummary 照常随候选发送，保留 IR 必须按实际发送的 5 条计（条数与 token 都含它）。
def test_non_handoff_compaction_summary_stays_in_retained_ir():
    context, params, frozen, _ = _fixture(seed=SimpleNamespace(compact_generation=2))
    _, projected = replace_recovery_active_tools(
        params, frozen, compact_context=context, retained_records=(),
        retained_ir_history=frozen.tool_ir_history, max_chars=2_000,
    )
    sent = sent_retained_ir(projected.tool_ir_history)
    assert [item.source for item in sent if isinstance(item, CompactionSummary)] == [""]
    assert len(sent) == len(projected.tool_ir_history) == 5
    actual = estimate_tokens(compact_tool_summary_text(compact_tool_summary_history((), projected.tool_ir_history)))
    assert retained_ir_facts(sent) == (5, actual)
    without_summary = tuple(item for item in sent if not isinstance(item, CompactionSummary))
    assert retained_ir_facts(without_summary)[1] < actual
    # 从来源保留区按同一口径取，结果与实际发送一致：旧摘要/旧交接不算，无来源载体照算。
    assert retained_ir_facts(sent_retained_ir(frozen.tool_ir_history)) == (5, actual)


# 真实联合替换入口仍守非空摘要合同；只有显式的只计量入口能投影“空摘要、无保留”（完整可计量由上面的宿主链用例证明）。
def test_real_mixed_callee_accepts_an_empty_summary_only_when_measuring():
    context, params, frozen, _ = _fixture()
    empty = replace(context, view=replace(context.view, summary=""))
    params = replace(params, compact_context=empty)
    call = partial(replace_recovery_active_tools, params, frozen, compact_context=empty, retained_records=(),
                   retained_ir_history=(), max_chars=2_000)
    with pytest.raises(ConversationCompactError) as error:
        call()
    assert error.value.code == "COMPACT_REQUEST_PROJECTION_UNKNOWN"
    measured_params, measured = call(measure_only=True)
    # 不插入空摘要载体，也不留任何旧交接或保留工具正文；原参数不被改动。
    assert measured.tool_ir_history == () and measured_params.tool_ir_history == []
    assert params.tool_ir_history == list(frozen.tool_ir_history)
