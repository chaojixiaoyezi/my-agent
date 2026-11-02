"""R9-25：子代理终态 canonical 首存与原 exact 收口事实同次落盘；之后的附带副作用失败时，重启后原监督恢复链补结算与父级唤醒。

真实 SimpleAgent、SubAgentManager、RuntimeDB、ConversationStore、原监督 sweep 与原恢复链；只把一次工作日志写入换成模拟 I/O 错误。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    supervise_stalled_orphans,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.runner_result_commit import (
    prepare_runner_result_commit,
)
from agent_py_agent.agent.subagents.services.runtime_closeout import (
    CLOSEOUT_UNPERSISTED_EVENT,
    pending_closeout,
)


def _agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")


# LLM: 孩子沿原 create_run 建立，并按 orchestration.lifecycle 的原写法把孩子 run 关联到父会话；attempt 由原 lifecycle 准备。
# 函数用途: 建一个直属根会话任务的孩子及其当前 exact attempt。
def _child_case(tmp_path):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "goal": "整理资料", "status": "active"})
    child = agent.subagents.create_run(goal="整理资料", thought="", plan=["执行"], parent_id="task-root", root_id="task-root")
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": child.id, "goal": child.goal,
                      "task_path": str(child.task_dir or ""), "status": "active"})
    attempt = agent.subagents.lifecycle.prepare_runner_attempt(child.id).runner_active_attempt_id
    assert attempt
    return agent, thread, child, attempt


def _parent_wakes(agent, thread_id, child_id):
    return [signal for signal in agent.conversation_store.wakes.pending(limit=0)
            if signal.thread_id == thread_id and signal.source_agent_id == child_id]


def _fail_first_runner_work_log(monkeypatch, manager):
    original = manager.actions._append_task_work_log
    failures = []

    def work_log(task, message, *args, **kwargs):
        if str(message).startswith("subagent_runner:") and not failures:
            failures.append(task.id)
            raise OSError("simulated I/O error")
        return original(task, message, *args, **kwargs)

    monkeypatch.setattr(manager.actions, "_append_task_work_log", work_log)
    return failures


def test_work_log_error_leaves_exact_root_handoff_recoverable_after_restart(tmp_path, monkeypatch):
    agent, thread, child, attempt = _child_case(tmp_path)
    failures = _fail_first_runner_work_log(monkeypatch, agent.subagents)
    params = RecordRunnerResultParams(run_id=child.id, dry_run=False, ok=True, message="资料已整理",
                                      attempt_id=attempt, status="DONE")
    with pytest.raises(OSError, match="simulated I/O error"):
        agent.subagents.runner_result.record_runner_result(params)
    assert failures == [child.id]
    # 首个终态保存已同时带上原 exact 收口事实；权威 run 尚未结算、父级尚未唤醒。
    saved = agent.subagents.load(child.id)
    assert saved.status == "DONE" and not saved.runner_active_attempt_id
    fact = pending_closeout(saved)
    assert fact is not None and (fact["attempt_id"], fact["delivery"]) == (attempt, "pending")
    assert fact["target_status"] == "DONE"
    repo = agent.subagents.runtime_db
    agent_run_id = repo.agent_run_for_run_id(child.id)["agent_run_id"]
    assert repo.agent_run_for_run_id(child.id)["status"] != "done"
    assert _parent_wakes(agent, thread.thread_id, child.id) == []

    # 重启：新的宿主实例只经原监督 sweep 推进；不得重跑业务或调模型。
    monkeypatch.setattr(SimpleAgent, "run_subagent",
                        lambda *_a, **_k: pytest.fail("closeout recovery must not rerun child business"))
    monkeypatch.setattr(SimpleAgent, "run", lambda *_a, **_k: pytest.fail("closeout recovery must not call the model"))
    restarted = _agent(tmp_path)
    summary = supervise_stalled_orphans(restarted)
    assert summary["runtime_closeouts_recovered"] == 1, summary
    (wake,) = _parent_wakes(restarted, thread.thread_id, child.id)
    assert wake.reason == "subagent_runner_finished" and wake.root_task_id == "task-root"
    assert wake.dedupe_key == f"subagent-finished:{child.id}:DONE:{attempt}"
    recovered = restarted.subagents.load(child.id)
    assert recovered.status == "DONE" and pending_closeout(recovered) is None
    repo = restarted.subagents.runtime_db
    assert repo.agent_run_for_run_id(child.id)["status"] == "done"
    assert [row["attempt_id"] for row in repo.attempts_for_run(agent_run_id)] == [attempt]

    # 重复恢复幂等：不再唤醒、不再结算、不新建 attempt。
    for _ in range(2):
        again = supervise_stalled_orphans(restarted)
        assert again["runtime_closeouts_recovered"] == 0 and again["runtime_closeouts_pending"] == 0, again
        assert [item.wake_signal_id for item in _parent_wakes(restarted, thread.thread_id, child.id)] == [wake.wake_signal_id]
    assert [row["attempt_id"] for row in repo.attempts_for_run(agent_run_id)] == [attempt]


def test_unpersisted_first_save_stops_after_original_fallback_event(tmp_path, monkeypatch):
    agent, thread, child, attempt = _child_case(tmp_path)
    manager = agent.subagents
    original_save = manager.save
    blocked = []

    def save(task):
        if task.id == child.id and pending_closeout(task) is not None and not blocked:
            raise OSError("simulated I/O error")
        return original_save(task)

    monkeypatch.setattr(manager, "save", save)
    logged = []
    monkeypatch.setattr(manager.actions, "_append_task_work_log", lambda task, message, *a, **k: logged.append(message))
    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=child.id, dry_run=False, ok=True, message="资料已整理", attempt_id=attempt, status="DONE"))
    assert result.status == "DONE"
    # 两次保存都失败：只留原兜底事件，不跑附带副作用、不结算、不通知。
    assert logged == []
    assert _parent_wakes(agent, thread.thread_id, child.id) == []
    repo = manager.runtime_db
    agent_run_id = repo.agent_run_for_run_id(child.id)["agent_run_id"]
    events = [row["event_type"] for row in repo.events_for_attempt(attempt, limit=100)]
    assert events.count(CLOSEOUT_UNPERSISTED_EVENT) == 1, events
    assert repo.agent_run_for_run_id(child.id)["status"] != "done"
    # 存储恢复后原恢复链先还原收口事实，再按原顺序结算并唤醒一次父级。
    blocked.append(True)
    restarted = _agent(tmp_path)
    for _ in range(3):
        supervise_stalled_orphans(restarted)
        if _parent_wakes(restarted, thread.thread_id, child.id):
            break
    (wake,) = _parent_wakes(restarted, thread.thread_id, child.id)
    assert wake.dedupe_key == f"subagent-finished:{child.id}:DONE:{attempt}"
    assert restarted.subagents.runtime_db.agent_run_for_run_id(child.id)["status"] == "done"
    assert [row["attempt_id"] for row in restarted.subagents.runtime_db.attempts_for_run(agent_run_id)] == [attempt]


def test_not_required_resumable_result_keeps_canonical_save_without_closeout(tmp_path):
    agent, _thread, child, attempt = _child_case(tmp_path)
    manager = agent.subagents
    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=child.id, dry_run=False, ok=False, message="等待授权后继续", attempt_id=attempt, status="BLOCKED",
        turn_end_reason="blocked", failure_type="capability_request"))
    assert result.status == "BLOCKED"
    saved = manager.load(child.id)
    assert saved.status == "BLOCKED"
    assert pending_closeout(saved) is None
    assert manager.runtime_db.agent_run_for_run_id(child.id)["status"] not in {"done", "failed"}


@pytest.mark.parametrize("case", ["terminal", "resumable", "unpersisted"])
def test_prepare_runner_result_commit_states(case):
    status = "PENDING" if case == "resumable" else "DONE"
    task = SubAgentTask(id="child", goal="g", thought="t", plan=[], status=status)
    params = RecordRunnerResultParams(run_id=task.id, dry_run=False, ok=True, message="", attempt_id="attempt-1",
                                      status=status)
    saved, events = [], []

    def save_task(current):
        if case == "unpersisted":
            raise OSError("simulated I/O error")
        saved.append((current.status, dict(pending_closeout(current) or {})))

    repo = SimpleNamespace(agent_run_for_run_id=lambda _run_id: {"agent_run_id": "agent-1"},
                           append_event=lambda **kwargs: events.append(kwargs["event_type"]))
    state = prepare_runner_result_commit(repo, task, params, SimpleNamespace(status=status, message=""),
                                         save_task=save_task)
    if case == "terminal":
        assert state == "persisted"
        assert len(saved) == 1 and saved[0][0] == "DONE"
        assert (saved[0][1]["attempt_id"], saved[0][1]["delivery"]) == ("attempt-1", "pending")
        assert events == []
    elif case == "resumable":
        assert state == "not_required" and saved == [] and events == []
    else:
        assert state == "unpersisted" and saved == []
        assert events == [CLOSEOUT_UNPERSISTED_EVENT]
