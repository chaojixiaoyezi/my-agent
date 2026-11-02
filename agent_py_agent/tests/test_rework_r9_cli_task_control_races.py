"""R9-12 调用方与竞态：PAUSED 与已落盘未投影的控制在启动预留、最终激活、协作选择、迟到启动回执、runner 心跳、
整份旧快照保存与窄心跳里都保持住；授权裁决不复活暂停的孩子；共享宿主上的兄弟不受影响；状态机把 PAUSED 登记为
非终态、不可派工、不可修复的等待。

真实 SimpleAgent、SubAgentManager、RuntimeDB、ConversationStore、原租约与原控制服务；控制中途失败用一次模拟 I/O 错误代替。
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    auto_start_orphan_run,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.collaboration_candidates import (
    _can_run_for_collaboration_request,
)
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    ResolveCapabilityRequestsTool,
)
from agent_py_agent.agent.agent_core.runner.session_pool import (
    RunnerSessionPoolLease,
    _relay_runner_stop,
    runner_session_lease,
)
from agent_py_agent.agent.concurrency.interrupt import is_interrupted, register_interruptible
from agent_py_agent.agent.contracts.state_machine import (
    RunStateFacts,
    can_dispatch,
    can_repair,
    lifecycle_phase,
    normalize_status,
    recovery_decision,
    run_state_snapshot_from_task,
    terminal_outcome,
    waiting_reason,
)
from agent_py_agent.agent.contracts.state_machine_transitions import transition_contract
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents import cancellation
from agent_py_agent.agent.subagents.process_control import BackgroundStartUpdate
from agent_py_agent.agent.subagents.recovery_eligibility import user_stopped_run_is_resumable
from agent_py_agent.agent.subagents.runner_control import runner_attempt_cancelled
from agent_py_agent.agent.subagents.runner_start import reserve_runner_start
from agent_py_agent.agent.subagents.services import lifecycle_runner_attempts
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
from agent_py_agent.agent.subagents.task_control import (
    TaskControlError,
    confirm_task_control_cleanup,
    control_task,
    task_control_record,
)
from agent_py_agent.agent.tooling.process_registry import capture_process_birth_token


def _agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")


# LLM: 孩子沿原 create_run 建立并按原写法关联到父会话；需要运行时由原 lifecycle 准备 exact attempt。
# 函数用途: 建一个直属根会话任务的孩子，返回 (agent, child, attempt)。
def _child(tmp_path, *, agent=None, running=True, goal="整理资料", **create_kwargs):
    agent = agent or _agent(tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    if store.tasks.load_report("task-root")[0] is None:
        store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "goal": goal, "status": "active"})
    child = agent.subagents.create_run(goal=goal, thought="", plan=["执行"], parent_id="task-root", root_id="task-root",
                                       **create_kwargs)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": child.id, "goal": child.goal,
                      "task_path": str(child.task_dir or ""), "status": "active"})
    attempt = agent.subagents.lifecycle.prepare_runner_attempt(child.id).runner_active_attempt_id if running else ""
    return agent, child, attempt


def _attempt_ids(agent, run_id):
    repo = agent.subagents.runtime_db
    return [row["attempt_id"] for row in repo.attempts_for_run(repo.agent_run_for_run_id(run_id)["agent_run_id"])]


def _lease(agent, run_id, attempt_id):
    lease = runner_session_lease(RunnerSessionPoolLease(
        manager=agent.subagents, run_id=run_id, attempt_id=attempt_id, interval_seconds=30.0,
    ))
    return lease, lease.__enter__()


def test_paused_run_is_blocked_at_reservation_activation_collaboration_and_late_receipts(tmp_path):
    agent, child, _ = _child(tmp_path, running=False)
    manager = agent.subagents
    pending = reserve_runner_start(manager, child.id)
    manager.lifecycle.update_background_start(child.id, BackgroundStartUpdate(
        launch_id="launch-1", status="launching", replace_launch=True, attempt_id=pending))
    attempt = manager.lifecycle.prepare_runner_attempt(child.id, expected_attempt_id=pending).runner_active_attempt_id
    assert attempt == pending
    assert control_task(agent, child.id, action="pause")["outcome"] == "applied"
    before = manager.load(child.id)
    attempts = _attempt_ids(agent, child.id)

    with pytest.raises(RuntimeConflictError):
        reserve_runner_start(manager, child.id)
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(child.id)
    for status in ("running", "finished"):
        with pytest.raises(RuntimeConflictError):
            manager.lifecycle.update_background_start(child.id, BackgroundStartUpdate(
                launch_id="launch-1", status=status, attempt_id=attempt))
    assert _can_run_for_collaboration_request(before) is False
    assert runner_attempt_cancelled(manager, child.id, attempt) is True
    assert auto_start_orphan_run(agent, child.id)["status"] == "not_needed"

    after = manager.load(child.id)
    assert after.status == "PAUSED" and after.runner_active_attempt_id == ""
    assert after.attributes["background_start"] == before.attributes["background_start"]
    assert task_control_record(after) == task_control_record(before)
    assert _attempt_ids(agent, child.id) == attempts


def test_pending_control_receipt_blocks_execution_until_original_control_reapplies(tmp_path, monkeypatch):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    lease, _session = _lease(agent, child.id, attempt)
    try:
        def interrupted(*_args, **_kwargs):
            raise OSError("simulated I/O error")

        # 回执已落盘、原撤权尚未执行时控制中断：回执保持 requested，并挡住全部执行入口。
        monkeypatch.setattr(cancellation, "prepare_subagent_stops", interrupted)
        with pytest.raises(OSError, match="simulated I/O error"):
            control_task(agent, child.id, action="pause")
        monkeypatch.undo()
        task = manager.load(child.id)
        control = task_control_record(task)
        assert (control["generation"], control["state"], control["cleanup"]) == (
            1, "requested", {"state": "pending", "attempt_id": attempt})
        assert task.status == "RUNNING" and task.runner_active_attempt_id == attempt
        assert manager.runtime_db.get_attempt(attempt)["status"] == "running"
        with pytest.raises(RuntimeConflictError):
            reserve_runner_start(manager, child.id)
        with pytest.raises(RuntimeConflictError):
            manager.lifecycle.prepare_runner_attempt(child.id)
        assert _can_run_for_collaboration_request(task) is False
        assert runner_attempt_cancelled(manager, child.id, attempt) is True

        # 重跑原控制：换代覆盖未生效回执，沿用同一执行轮的清理义务，再走原撤权与投影。
        report = control_task(agent, child.id, action="pause")
        assert (report["outcome"], report["control_generation"], report["cleanup_state"]) == ("applied", 2, "pending")
        task = manager.load(child.id)
        assert task.status == "PAUSED" and manager.runtime_db.get_attempt(attempt)["status"] == "cancelled"
    finally:
        lease.__exit__(None, None, None)
    assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "confirmed"


def test_activation_reducer_rechecks_control_that_lands_after_admission(tmp_path, monkeypatch):
    agent, child, _ = _child(tmp_path, running=False)
    manager = agent.subagents
    original = lifecycle_runner_attempts._recover_unsubmitted_agent_guidance

    # 准入复核之后、最终激活 reducer 之前落盘一份 requested 控制回执。
    def guidance_then_control(manager_arg, task, attempt_id):
        original(manager_arg, task, attempt_id)

        def request(current):
            current.attributes = {**dict(current.attributes or {}), "task_control": {
                "schema_version": "subagent_task_control.v1", "generation": 1, "action": "pause",
                "target_status": "PAUSED", "state": "requested", "run_id": current.id,
            }}

        manager_arg.mutate(task.id, request)

    monkeypatch.setattr(lifecycle_runner_attempts, "_recover_unsubmitted_agent_guidance", guidance_then_control)
    with pytest.raises(RuntimeConflictError):
        manager.lifecycle.prepare_runner_attempt(child.id)
    task = manager.load(child.id)
    assert task.status != "RUNNING" and task.runner_active_attempt_id == ""


def test_heartbeat_consumes_exact_control_and_only_owning_attempt_confirms_cleanup(tmp_path):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    lease, session = _lease(agent, child.id, attempt)
    try:
        assert control_task(agent, child.id, action="pause")["cleanup_state"] == "pending"
        # 控制所在进程没有原执行令牌；原执行轮所在进程的心跳读到精确回执后才中转中断。
        ready, release, seen = threading.Event(), threading.Event(), {}

        def execution():
            with register_interruptible(f"subagent-runner-attempt:{child.id}:{attempt}"):
                ready.set()
                release.wait(10)
                seen["interrupted"] = is_interrupted()

        worker = threading.Thread(target=execution, daemon=True)
        worker.start()
        assert ready.wait(10)
        assert _relay_runner_stop(RunnerSessionPoolLease(manager=manager, run_id=child.id, attempt_id=attempt)) is True
        release.set()
        worker.join(10)
        assert seen == {"interrupted": True}
        # 别的执行轮或旧会话不能代为确认。
        other = {**session, "session_id": "runsess-other", "attempt_id": "attempt-other"}
        assert confirm_task_control_cleanup(manager, child.id, attempt_id="attempt-other", session=other,
                                            runner_status="completed") is False
        assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "pending"
        with pytest.raises(TaskControlError) as refused:
            control_task(agent, child.id, action="resume")
        assert refused.value.code == "TASK_CLEANUP_UNCONFIRMED"
    finally:
        lease.__exit__(None, None, None)
    cleanup = task_control_record(manager.load(child.id))["cleanup"]
    assert (cleanup["state"], cleanup["session_id"], cleanup["runner_exit"]) == ("confirmed", session["session_id"], "completed")
    assert confirm_task_control_cleanup(manager, child.id, attempt_id=attempt, session=session,
                                        runner_status="completed") is False


def test_stale_full_saves_preserve_control_generation_and_positive_cleanup(tmp_path):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    before_control = manager.load(child.id)
    lease, _session = _lease(agent, child.id, attempt)
    control_task(agent, child.id, action="pause")
    during_pause = manager.load(child.id)
    paused_control = task_control_record(during_pause)

    # 控制前读取的整份快照：普通字段照常合并，控制投影与回执保持 canonical。
    before_control.latest_summary = "旧进度"
    manager.save(before_control)
    task = manager.load(child.id)
    assert task.latest_summary == "旧进度"
    assert task.status == "PAUSED" and task.runner_active_attempt_id == "" and attempt in task.runner_abandoned_attempt_ids
    assert task_control_record(task) == paused_control

    lease.__exit__(None, None, None)
    assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "confirmed"
    # 同代旧快照（清理确认前读取）不能把正向清理洗回 pending。
    manager.save(during_pause)
    assert task_control_record(manager.load(child.id))["cleanup"]["state"] == "confirmed"

    assert control_task(agent, child.id, action="resume")["status"] == "PENDING"
    manager.save(during_pause)
    task = manager.load(child.id)
    assert task.status == "PENDING" and task_control_record(task)["generation"] == 2


def test_narrow_runner_session_updates_keep_control_and_fence_old_attempt(tmp_path):
    agent, child, attempt = _child(tmp_path)
    manager = agent.subagents
    lease, session = _lease(agent, child.id, attempt)
    try:
        control_task(agent, child.id, action="pause")
        paused_control = task_control_record(manager.load(child.id))
        now = time.time()
        assert manager.save_runner_session(child.id, {**session, "status": "running"}, now=now) is False
        assert task_control_record(manager.load(child.id)) == paused_control
        # 窄心跳本身不是清理证明；原会话最终回执照常落盘，回执不变。
        assert manager.save_runner_session(child.id, {**session, "status": "failed", "ended_at": now}, now=now) is True
        task = manager.load(child.id)
        assert task_control_record(task) == paused_control and task.status == "PAUSED"
    finally:
        lease.__exit__(None, None, None)


@pytest.mark.parametrize("decision", ["grant", "deny"])
def test_capability_resolution_keeps_paused_child_paused_without_reopening_link(tmp_path, decision):
    # 创建该孩子的父回合快照含 read_file（原 direct_parent_tool_authority 权威来源）。
    agent, child, _ = _child(tmp_path, running=False, attributes={"direct_parent_tool_authority": {
        "schema_version": "direct_parent_tool_authority.v1", "parent_run_id": "task-root",
        "available_tool_names": ["read_file"], "snapshot_hash": "sha256:exact", "owner_type": "main_agent"}})
    manager = agent.subagents
    request = manager.lifecycle.record_capability_request(child.id, RecordCapabilityRequestParams(
        problem="当前快照没有 read_file", needed_capability="read_file", capability_type="tool",
        requested_tools=["read_file"]))
    assert control_task(agent, child.id, action="pause")["outcome"] == "applied"
    result = ResolveCapabilityRequestsTool(agent).execute({
        "run_id": child.id, "request_id": request.id, "decision": decision, "reason": "父级裁决"})
    assert "run_paused_by_control" in result.output, result.output
    task = manager.load(child.id)
    # 裁决审计保留（授予落原 grant 账，拒绝落协议终态 CLOSED 与原因）；暂停不被改成 PENDING，父会话链接仍是 interrupted。
    if decision == "grant":
        assert task.capability_requests[0].status == "GRANTED"
        assert [grant.tools for grant in task.capability_grants] == [["read_file"]]
    else:
        assert task.capability_requests[0].status == "CLOSED"
        assert task.capability_requests[0].constraints["denial_reason"] == "父级裁决"
    assert task.status == "PAUSED" and "capability_resolution_continuation" not in task.attributes
    assert agent.conversation_store.tasks.load_report(child.id)[0].status == "interrupted"
    assert task_control_record(task)["state"] == "applied"


def test_pause_spares_sibling_sharing_the_runner_host(tmp_path):
    agent, first, first_attempt = _child(tmp_path)
    _, sibling, sibling_attempt = _child(tmp_path, agent=agent)
    manager = agent.subagents
    now, pid = time.time(), os.getpid()

    # 两个孩子由同一个派工子进程宿主承载（同 launch、同 pid）。
    def shared_host(attempt_id):
        def record(current):
            current.attributes = {
                **dict(current.attributes or {}),
                "background_start": {"launch_id": "launch-shared", "attempt_id": attempt_id, "pid": pid,
                                     "status": "running", "updated_at": now},
                "runner_session": {"session_id": f"runsess-{current.id}", "run_id": current.id, "attempt_id": attempt_id,
                                   "launch_id": "launch-shared", "worker_pid": pid,
                                   "worker_birth_token": capture_process_birth_token(pid), "in_process": False,
                                   "status": "running", "heartbeat_at": now, "interval_seconds": 30.0},
            }
        return record

    manager.mutate(first.id, shared_host(first_attempt))
    manager.mutate(sibling.id, shared_host(sibling_attempt))
    report = control_task(agent, first.id, action="pause")
    assert report["outcome"] == "applied"
    pid_report = report["local_cleanup"]["pid_report"]
    assert pid_report["status"] == "shared_host_cooperative" and pid_report["escalated"] is False
    assert pid_report["shared_run_ids"] == [sibling.id]
    untouched = manager.load(sibling.id)
    assert untouched.status == "RUNNING" and untouched.runner_active_attempt_id == sibling_attempt
    assert manager.runtime_db.get_attempt(sibling_attempt)["status"] == "running"
    assert task_control_record(untouched) == {}


def test_paused_is_not_a_user_stop_and_user_stop_is_not_resumed_by_task_control(tmp_path):
    agent, child, attempt = _child(tmp_path)
    lease, _ = _lease(agent, child.id, attempt)
    control_task(agent, child.id, action="pause")
    lease.__exit__(None, None, None)
    assert user_stopped_run_is_resumable(agent.subagents.load(child.id)) is False
    _, stopped, _ = _child(tmp_path, agent=agent, goal="另一项")
    cancellation.cancel_subagent_tree(agent, cancellation.CancelSubagentTaskRequest(
        agent.subagents.load(stopped.id), "conversation_user_stop", kill_process=False))
    with pytest.raises(TaskControlError) as refused:
        control_task(agent, stopped.id, action="resume")
    assert refused.value.code == "TASK_NOT_PAUSED"
    assert agent.subagents.load(stopped.id).status == "CANCELLED"


def test_state_machine_registers_paused_as_nonterminal_non_repairable_wait():
    facts = RunStateFacts(status="PAUSED", channel_status="BROKEN", attempts=0, max_attempts=3)
    assert normalize_status("PAUSED") == "PAUSED"
    assert can_dispatch(facts) is False and can_dispatch(facts, force=True) is False
    assert can_repair(facts) is False
    assert waiting_reason(facts) == "paused" and lifecycle_phase(facts) == "PAUSED"
    assert terminal_outcome(facts) == "active"
    decision = recovery_decision(facts)
    assert (decision.action, decision.allow_new_run, decision.reason) == ("wait", False, "paused")
    snapshot = run_state_snapshot_from_task(type("Task", (), {"id": "run-1", "status": "PAUSED"})())
    assert snapshot["status"] == "PAUSED" and snapshot["status_protocol_error"] == ""
    assert transition_contract("RUNNING", "PAUSED").allowed
    resume = transition_contract("PAUSED", "PENDING")
    assert resume.allowed and resume.required_condition == "explicit_resume_after_exact_cleanup"
    assert not transition_contract("PAUSED", "RUNNING").allowed
    assert transition_contract("PAUSED", "ABANDONED").allowed
    assert not transition_contract("DONE", "PAUSED").allowed
