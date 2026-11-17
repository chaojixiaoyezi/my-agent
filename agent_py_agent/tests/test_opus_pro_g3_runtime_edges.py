"""G3 运行时边角：Audit 子代理谱系（R06）、规划失败退避（R15）、S11 过期停放标记（N01）、
自由线程 GC 计量快照（N02）、封顶触发线下的闲置压缩水位（O08）。"""
from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.subagent import run_flow
from agent_py_agent.agent.agent_core.subagent.params import SubagentRunParams
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    audit_lineage_task_id,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.compact import ConversationContextUsage
from agent_py_agent.agent.conversation.technical_continuation import (
    mark_user_input_yield_parked,
    user_input_yield_parked_task_id,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.ingestion.watch_tool import _audit_parent_open_error
from agent_py_agent.agent.observability import gc_pause_meter
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli import gateway_loops as loops

_AUDIT_ROOT = "audit-root-r06"


# 函数用途: 建一个带真实会话账本的 Agent，并在父会话上登记一条 active 的命名 Audit。
def _agent_with_active_audit(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_home=str(tmp_path / "home")),
                        tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "local/main", "channel": "tui",
                                          "channel_conversation_id": "audit-chat", "channel_user_id": "local-user",
                                          "cwd": str(tmp_path)})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": _AUDIT_ROOT, "goal": "持续研判来源",
                      "status": "active", "work_kind": "audit", "work_name": "来源研判"})
    return agent, thread


# 函数用途: 直接进入 _run_and_finalize_subagent，在模型轮内记录当前谱系与父 Audit 复核结果，不调用模型。
def _lineage_inside_child_turn(agent, monkeypatch, attributes, *, thread_id):
    task = agent.subagents.create_run(goal="研判一条来源", thought="", plan=["read"], role="worker",
                                      root_id=_AUDIT_ROOT,
                                      attributes={"conversation_thread_id": thread_id, **attributes})
    seen = {}

    def model_turn(lifecycle, prompt, context, task_attributes, *, task, attempt_id):
        lineage = audit_lineage_task_id(lifecycle.agent)
        seen.update(lineage=lineage, attempt_id=attempt_id,
                    request_id=task_attributes.get(CONVERSATION_REQUEST_ID_ATTR),
                    parent_error=_audit_parent_open_error(lifecycle.agent, lineage))
        return "child-result"

    def failed(params):
        raise AssertionError(f"child turn failed: {params.error!r}")

    monkeypatch.setattr(run_flow, "_run_subagent_model_turn", model_turn)
    lifecycle = SimpleNamespace(agent=agent, finalize_run=lambda params: params, handle_run_failure=failed)
    bundle = run_flow.SubagentModelTurnBundle(SubagentRunParams(run_id=task.id), "attempt-r06-1", None, "研判")
    finalized = run_flow._run_and_finalize_subagent(lifecycle, bundle)
    assert finalized.result == "child-result"
    return seen


# 函数用途: R06——来源工作者在本轮内仍以 Audit 根任务为谱系，开启来源时父任务复核通过。
def test_audit_source_worker_keeps_the_named_audit_lineage_inside_its_turn(tmp_path, monkeypatch):
    agent, thread = _agent_with_active_audit(tmp_path)
    seen = _lineage_inside_child_turn(agent, monkeypatch, {
        AUDIT_ATTR: True, AUDIT_SOURCE_WORKER_ATTR: True, AUDIT_SOURCE_ID_ATTR: "source-a",
        CONVERSATION_REQUEST_ID_ATTR: _AUDIT_ROOT, "conversation_task_id": _AUDIT_ROOT,
    }, thread_id=thread.thread_id)
    assert seen["attempt_id"] == "attempt-r06-1"
    assert seen["lineage"] == _AUDIT_ROOT
    assert seen["request_id"] == _AUDIT_ROOT
    assert seen["parent_error"] is None


# 函数用途: R06——普通子代理与缺 Audit 根编号的子代理照旧以本次 attempt 作会话请求编号。
@pytest.mark.parametrize("attributes", [
    {CONVERSATION_REQUEST_ID_ATTR: "parent-request-1"},
    {AUDIT_ATTR: True},
    {AUDIT_ATTR: False, CONVERSATION_REQUEST_ID_ATTR: _AUDIT_ROOT},
])
def test_other_subagents_still_use_their_attempt_id(tmp_path, monkeypatch, attributes):
    agent, thread = _agent_with_active_audit(tmp_path)
    seen = _lineage_inside_child_turn(agent, monkeypatch, attributes, thread_id=thread.thread_id)
    assert seen["request_id"] == seen["lineage"] == "attempt-r06-1"


# 函数用途: R15——规划持续失败的 owner 只报一次错，下一次规划推迟到未来；之后到点重试并翻倍退避。
def test_failing_owner_plan_backs_off_instead_of_logging_every_tick(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_gateway_owner_idle_backoff import _supervisor

    supervisor, scheduler, clock, calls, submitted = _supervisor(tmp_path, monkeypatch)
    errors = []
    monkeypatch.setattr(loops, "_print_gateway_loop_error", lambda context, worker_id, exc: errors.append(context))

    def unreadable_owner(**kwargs):
        calls.append(clock[0])
        raise OSError("owner home is gone")

    scheduler.prepare_tick = unreadable_owner
    for _ in range(5):
        supervisor._submit_ready_thread_ticks()
    assert errors == ["gateway_background_main.plan"]
    assert calls == [100.0]
    assert supervisor._owner_next_check_at["base"] == pytest.approx(101.0)
    clock[0] = 101.0
    supervisor._submit_ready_thread_ticks()
    assert len(errors) == 2
    assert supervisor._owner_next_check_at["base"] == pytest.approx(103.0)
    assert submitted == []


# 函数用途: R15——上限为 0 时仍每拍规划并照常报错，不留退避截止点。
def test_zero_cap_keeps_planning_every_tick_after_a_failure(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_gateway_owner_idle_backoff import _supervisor

    supervisor, scheduler, _clock, _calls, _submitted = _supervisor(tmp_path, monkeypatch, cap=0.0)
    errors = []
    monkeypatch.setattr(loops, "_print_gateway_loop_error", lambda context, worker_id, exc: errors.append(context))

    def unreadable_owner(**kwargs):
        raise OSError("owner home is gone")

    scheduler.prepare_tick = unreadable_owner
    for _ in range(5):
        supervisor._submit_ready_thread_ticks()
    assert errors == ["gateway_background_main.plan"] * 5
    assert supervisor._owner_next_check_at.get("base", 0.0) == 0.0


# 函数用途: 跑一条真实定时执行到“为排队的用户回合让出车道”，返回停放中的现场。
def _parked_scheduled_case(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import run_claim
    from agent_py_agent.tests.test_w3_s11_user_input_yield import _scheduled_case, _yield_wakes

    case = _scheduled_case(tmp_path, monkeypatch, tool_rounds=30)
    key = run_claim._user_input_lane_key(case.store, case.thread.thread_id)

    def user_arrives(call_count):
        if call_count == 2:
            run_claim._register_user_input_turn(key)

    case.on_call = user_arrives
    try:
        case.scheduler.tick(now=1_001)
    finally:
        run_claim._adjust_user_input_turns(key, -1)
    case.on_call = None
    assert user_input_yield_parked_task_id(case.store.threads.require(case.thread.thread_id)) == case.run_id
    assert len(_yield_wakes(case)) == 1
    return case, _yield_wakes


# 函数用途: N01——用户在让出期间停止了这条定时执行：作废的续跑唤醒收口时撤下线程上的停放标记。
def test_stale_yield_continuation_of_a_stopped_task_clears_the_parked_marker(tmp_path, monkeypatch):
    case, yield_wakes = _parked_scheduled_case(tmp_path, monkeypatch)
    case.store.tasks.update_status({"task_id": case.run_id, "status": "cancelled", "now": 1_002})
    case.scheduler.tick(now=1_003)
    assert len(case.calls) == 2
    assert yield_wakes(case) == []
    assert user_input_yield_parked_task_id(case.store.threads.require(case.thread.thread_id)) == ""


# 函数用途: N01——停放任务的链接已不存在（任务被删除）：续跑唤醒在执行入口作废时同样撤下标记。
def test_stale_yield_continuation_of_a_deleted_task_clears_the_parked_marker(tmp_path, monkeypatch):
    case, yield_wakes = _parked_scheduled_case(tmp_path, monkeypatch)
    case.store.storage.task_path(case.run_id).unlink()
    case.scheduler.tick(now=1_003)
    assert len(case.calls) == 2
    assert yield_wakes(case) == []
    assert user_input_yield_parked_task_id(case.store.threads.require(case.thread.thread_id)) == ""


# 函数用途: 建一个普通网关会话，并在线程上留下一条指向已停止定时执行的过期停放标记及其半程记录。
def _thread_with_stale_marker(tmp_path):
    from agent_py_agent.tests.test_gateway_chat_conversation_context import _conversation_context

    tmp_path.mkdir(parents=True, exist_ok=True)
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {"conversation": {"channel": "chat", "channel_conversation_id": "s11-stale",
                                "channel_user_id": "local-cli", "canonical_user_id": "local-agent"}}
    first = _conversation_context(agent, request, "gw-first", "看一下巡检结果")
    store = agent.conversation_store
    store.tasks.bind({"thread_id": first.thread_id, "task_id": "srun_done", "goal": "执行巡检", "now": 1_000})
    for role, content, metadata in (("user", "看一下巡检结果", {}),
                                     ("assistant", "巡检读到 sample_02。", {"task_id": "srun_done"})):
        store.messages.append({"thread_id": first.thread_id, "role": role, "content": content,
                               "metadata": metadata, "now": 1_001})
    assert mark_user_input_yield_parked(store, first.thread_id, "srun_done") is True
    store.tasks.update_status({"task_id": "srun_done", "status": "cancelled", "now": 1_002})
    return agent, request, store.threads.require(first.thread_id)


# 函数用途: N01——读方只认仍 active 的停放任务；过期标记不再把已停止任务的记录挡在用户回合历史之外。
def test_user_turn_history_ignores_a_parked_marker_of_a_stopped_task(tmp_path):
    from agent_py_agent.tests.test_gateway_chat_conversation_context import _conversation_context

    agent, request, thread = _thread_with_stale_marker(tmp_path)
    assert user_input_yield_parked_task_id(thread) == "srun_done"
    second = _conversation_context(agent, request, "gw-second", "巡检读到哪了？")
    contents = [row.content for row in second.history_source.rows]
    assert "巡检读到 sample_02。" in contents


# 函数用途: N01——压缩恢复候选的历史投影同样不认过期停放标记。
def test_compact_recovery_candidate_ignores_a_parked_marker_of_a_stopped_task(tmp_path, monkeypatch):
    from agent_py_agent.agent import gateway_compact_recovery as recovery

    agent, request, thread = _thread_with_stale_marker(tmp_path)
    seen = []

    class _Captured(Exception):
        pass

    def history_source(rows, request_id, work_scope, *, parked_task_id=""):
        seen.append(parked_task_id)
        raise _Captured

    monkeypatch.setattr(recovery.request_context, "_gateway_history_source", history_source)
    context = SimpleNamespace(agent=agent, request=request, request_id="gw-recover")
    conversation = SimpleNamespace(compact_source=SimpleNamespace(thread=thread))
    with pytest.raises(_Captured):
        recovery._project_recovery_candidate(context, conversation, None, None,
                                             SimpleNamespace(is_candidate=True, messages=()))
    assert seen == [""]


# 函数用途: N01——停放任务仍 active 时标记照旧生效，已停止时读方当它不存在；任务链接读不出时保留标记的保护。
def test_parked_marker_still_protects_an_active_or_unreadable_task(tmp_path):
    from agent_py_agent.agent.conversation.technical_continuation import (
        active_user_input_yield_parked_task_id,
    )

    stale_agent, _request, stale_thread = _thread_with_stale_marker(tmp_path / "stale")
    assert active_user_input_yield_parked_task_id(stale_agent.conversation_store, stale_thread) == ""
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "feishu",
                                          "channel_conversation_id": "s11-active", "channel_user_id": "open-1"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "srun_live", "goal": "执行巡检", "now": 1_000})
    assert mark_user_input_yield_parked(store, thread.thread_id, "srun_live") is True
    thread = store.threads.require(thread.thread_id)
    assert active_user_input_yield_parked_task_id(store, thread) == "srun_live"

    def unreadable(_task_id):
        raise OSError("task link unavailable")

    broken = SimpleNamespace(tasks=SimpleNamespace(load=unreadable))
    assert active_user_input_yield_parked_task_id(broken, thread) == "srun_live"
    assert active_user_input_yield_parked_task_id(None, thread) == "srun_live"


class _MutatedDuringIteration(deque):
    def __iter__(self):
        raise RuntimeError("deque mutated during iteration")


# 函数用途: N02——快照撞上并发追加时返回未知（None），不把 RuntimeError 抛进模型调用记录。
def test_gc_pause_snapshot_race_reports_unknown(monkeypatch):
    monkeypatch.setitem(gc_pause_meter._state, "installed", True)
    monkeypatch.setattr(gc_pause_meter, "_recent", deque([(1.0, 1.5)], maxlen=4))
    assert gc_pause_meter.pause_ms_between(0.5, 2.0) == 500.0
    monkeypatch.setattr(gc_pause_meter, "_recent", _MutatedDuringIteration([(1.0, 1.5)], maxlen=4))
    assert gc_pause_meter.pause_ms_between(0.5, 2.0) is None


# 函数用途: 建一条已闲置、无后台工作的真实会话，供闲置压缩门槛判定。
def _idle_candidate(tmp_path, monkeypatch, usage):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import compact_operations as idle

    store = ConversationStore(tmp_path / "conversations")
    row = store.threads.create(canonical_user_id="test-user")
    row = store.threads.update_atomic(row.thread_id, lambda current: replace(current, metadata={
        "last_user_activity_at": 100.0}))
    agent = SimpleNamespace(config=AgentConfig(), home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    calls = []
    monkeypatch.setattr(idle, "idle_compact_busy_state", lambda *_: "")
    monkeypatch.setattr(idle, "inspect_conversation_context", lambda *_: usage["current"])
    monkeypatch.setattr(idle, "prepare_conversation_context",
                        lambda *args, **kwargs: calls.append(kwargs) or SimpleNamespace(compacted=True))
    return idle, agent, store, row, calls


# 函数用途: 生成被绝对上限封顶的真实用量快照：1M 窗口、普通触发线 150K。
def _capped_usage(projected_tokens):
    return ConversationContextUsage(projected_tokens=projected_tokens, context_window_tokens=1_000_000,
                                    trigger_percent=80, trigger_tokens=150_000, compact_generation=0,
                                    compact_source_messages=0, compact_source_tool_pairs=0,
                                    terminal_tool_fold_turns=0, terminal_tool_fold_calls=0, pending_messages=3,
                                    has_summary=False, trigger_capped=True)


# 函数用途: O08——闲置水位取封顶后触发线的 60%（90K），而不是窗口×百分比的 480K。
def test_idle_compact_threshold_follows_the_capped_trigger(tmp_path, monkeypatch):
    usage = {"current": _capped_usage(89_999)}
    idle, agent, store, row, calls = _idle_candidate(tmp_path, monkeypatch, usage)
    assert idle._compact_idle_candidate(agent, store, row, now=7_300)["reason"] == "below_threshold"
    usage["current"] = _capped_usage(90_000)
    assert idle._compact_idle_candidate(agent, store, row, now=7_300)["reason"] == "compacted"
    assert len(calls) == 1


# 函数用途: O08——到达封顶触发线且普通压缩已覆盖同一活动代与消息尾时，闲置压缩让给普通压缩。
def test_idle_compact_yields_to_regular_compaction_at_the_capped_trigger(tmp_path, monkeypatch):
    from dataclasses import replace

    idle, agent, store, row, calls = _idle_candidate(tmp_path, monkeypatch, {"current": _capped_usage(150_000)})
    marker, errors = store.messages.tail_marker_report(row.thread_id)
    assert not errors
    row = store.threads.update_atomic(row.thread_id, lambda current: replace(current, metadata={
        **current.metadata, "last_compacted_user_activity_gen": 0, "last_compacted_message_tail": marker}))
    assert idle._compact_idle_candidate(agent, store, row, now=7_300)["reason"] == "regular_compacted"
    assert calls == []
