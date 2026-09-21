"""固定原子树停止的开发合同；临时真实账本与受控线程，不替代实际 TUI。"""
from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.local_run_control import (
    LocalRunControl,
    prepare_local_task_stop,
)
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _GatewayRequestRecord,
    _stop_active_task,
)
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.subagents import cancellation
from agent_py_agent.agent.subagents.cancellation import (
    CancelSubagentTaskRequest,
    cleanup_subagent_stops,
    prepare_subagent_stops,
)
from agent_py_agent.agent.tooling import process_resource_stop
from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanup
from agent_py_agent.tests.test_task_resource_stop import _write
from agent_py_agent.tests.test_task_resource_stop import main_task as main_task


# LLM: 只使用临时管理器正式创建/激活；资源以原 Store 记录声明，不启动真实模型或任务进程。
# 函数用途: 构造具备真实执行轮和会话归属的子代理供停止控制验证。
def _child(agent, scope, *, parent_id="main"):
    task = agent.subagents.create_run(
        goal="子树停止开发验证", thought="测试", plan=["读取资料"], parent_id=parent_id, root_id="main",
        attributes={
            "conversation_task_id": "task", "conversation_thread_id": scope.thread_id,
            CONVERSATION_REQUEST_ID_ATTR: "request",
        },
    )
    agent.conversation_store.tasks.bind({
        "thread_id": scope.thread_id, "task_id": task.id, "goal": task.goal, "status": "active",
    })
    return agent.subagents.lifecycle.prepare_runner_attempt(task.id)


def _request(task, *, kill_process=True):
    return CancelSubagentTaskRequest(task, "conversation_user_stop", kill_process, "test")


def _resource_scope(scope, task):
    return replace(scope, run_id=task.id, attempt_id=task.runner_active_attempt_id)


def _record_cleanups(monkeypatch):
    selected = []

    def clean(_store, record):
        selected.append(record["session_id"])
        return ProcessSessionCleanup(record, True)

    monkeypatch.setattr(process_resource_stop, "stop_process_session", clean)
    return selected


def test_tree_batch_excludes_resumed_attempt_new_descendant_and_other_tree(main_task, monkeypatch):
    agent, _link, _record, scope, store = main_task
    child = _child(agent, scope)
    grandchild = _child(agent, scope, parent_id=child.id)
    unrelated = _child(agent, scope, parent_id="other-root")
    _write(store, _resource_scope(scope, child), "bg-child")
    _write(store, _resource_scope(scope, grandchild), "bg-grandchild")
    _write(store, _resource_scope(scope, unrelated), "bg-unrelated")
    batch = prepare_subagent_stops(agent, [_request(agent.subagents.load(child.id))])
    assert not batch.unconfirmed
    assert {stop.report["run_id"] for stop in batch.stops} == {child.id, grandchild.id}
    assert all(agent.subagents.load(run_id).status == "CANCELLED" for run_id in (child.id, grandchild.id))
    resumed = agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    later = _child(agent, scope, parent_id=child.id)
    _write(store, _resource_scope(scope, resumed), "bg-resumed")
    _write(store, _resource_scope(scope, later), "bg-later")
    selected = _record_cleanups(monkeypatch)
    result = cleanup_subagent_stops(batch)
    assert result["ok"]
    assert set(selected) == {"bg-child", "bg-grandchild"}
    assert all(not store.load(sid).record["stop_requested"] for sid in ("bg-unrelated", "bg-resumed", "bg-later"))
    assert all(agent.subagents.load(run_id).status == "RUNNING" for run_id in (child.id, later.id, unrelated.id))


@pytest.mark.parametrize("status", ["done", "failed", "unknown"])
def test_terminal_child_leftover_resources_are_selected_without_rewriting_history(main_task, monkeypatch, status):
    agent, _link, _record, scope, store = main_task
    child = _child(agent, scope)
    child_scope = _resource_scope(scope, child)
    _write(store, child_scope, "bg-leftover")
    repo = agent.subagents.runtime_db
    run = repo.agent_run_for_run_id(child.id)
    if status == "unknown":
        repo._mark_attempt_unknown(child.runner_active_attempt_id, run["agent_run_id"], reason="uncertain", operator="test")
        child.status = "ABANDONED"
    else:
        repo.settle_agent_run(agent_run_id=run["agent_run_id"], attempt_id=child.runner_active_attempt_id, status=status)
        child.status = status.upper()
    child.runner_active_attempt_id = ""
    agent.subagents.save(child)
    before = dict(repo.get_attempt(child_scope.attempt_id))
    with repo._runtime_connection() as conn:
        locks = [dict(row) for row in conn.execute("SELECT * FROM resource_locks ORDER BY lock_id")]
    batch = prepare_subagent_stops(agent, [_request(agent.subagents.load(child.id))])
    assert store.load("bg-leftover").record["stop_requested"]
    _record_cleanups(monkeypatch)
    assert cleanup_subagent_stops(batch)["ok"]
    loaded = agent.subagents.load(child.id)
    assert loaded.status == child.status and "cancel_subagents" not in loaded.attributes
    assert dict(repo.get_attempt(child_scope.attempt_id)) == before
    with repo._runtime_connection() as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM resource_locks ORDER BY lock_id")] == locks


def test_stale_original_request_cannot_close_new_attempt_or_its_resources(main_task):
    agent, _link, _record, scope, store = main_task
    child = _child(agent, scope)
    prepare_subagent_stops(agent, [_request(agent.subagents.load(child.id))])
    resumed = agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    _write(store, _resource_scope(scope, resumed), "bg-new")
    with pytest.raises(RuntimeConflictError):
        prepare_subagent_stops(agent, [_request(child)])
    assert agent.subagents.load(child.id).runner_active_attempt_id == resumed.runner_active_attempt_id
    assert not store.load("bg-new").record["stop_requested"]


def test_kill_process_false_does_not_request_background_or_pty_cleanup(main_task, monkeypatch):
    agent, _link, _record, scope, store = main_task
    child = _child(agent, scope)
    _write(store, _resource_scope(scope, child), "bg-keep")
    monkeypatch.setattr(process_resource_stop.pty_session_registry, "request_stop", lambda **kw: pytest.fail("不得停 PTY"))
    batch = prepare_subagent_stops(agent, [_request(child, kill_process=False)])
    assert batch.stops[0].resources is None
    assert not store.load("bg-keep").record["stop_requested"]
    assert agent.subagents.load(child.id).status == "CANCELLED"


def test_partial_authority_failure_keeps_other_members_and_committed_resources(main_task, monkeypatch):
    agent, _link, _record, scope, store = main_task
    child = _child(agent, scope)
    grandchild = _child(agent, scope, parent_id=child.id)
    _write(store, _resource_scope(scope, child), "bg-root")
    _write(store, _resource_scope(scope, grandchild), "bg-grandchild")
    original = cancellation._close_authority

    def close(manager, request):
        if request.task.id == grandchild.id:
            raise OSError("controlled failure")
        return original(manager, request)

    monkeypatch.setattr(cancellation, "_close_authority", close)
    batch = prepare_subagent_stops(agent, [_request(agent.subagents.load(child.id))])
    assert batch.unconfirmed and batch.failed[0]["run_id"] == grandchild.id
    selected = _record_cleanups(monkeypatch)
    assert not cleanup_subagent_stops(batch)["ok"]
    assert selected == ["bg-root"]
    assert not store.load("bg-grandchild").record["stop_requested"]
    assert agent.subagents.load(grandchild.id).status == "RUNNING"


@pytest.mark.parametrize("surface", ["gateway", "local"])
def test_controls_freeze_children_before_slow_main_cleanup(main_task, monkeypatch, surface):
    from agent_py_agent.agent.conversation.task_resources import cleanup_task_resources

    agent, link, record, scope, store = main_task
    child = _child(agent, scope)
    _write(store, scope, "bg-main")
    _write(store, _resource_scope(scope, child), "bg-child")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    selected = []

    def clean(_store, frozen):
        selected.append(frozen["session_id"])
        if frozen["session_id"] == "bg-main":
            entered.set()
            assert release.wait(4)
        if frozen["session_id"] == "bg-child":
            done.set()
        return ProcessSessionCleanup(frozen, True)

    monkeypatch.setattr(process_resource_stop, "stop_process_session", clean)
    try:
        if surface == "gateway":
            active = _GatewayRequestRecord(None, {
                "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
            }, target_kind="task")
            result = _stop_active_task(agent, active, GatewayControlScope("tester", "chat", "test-session"))
            assert result.ok, result
        else:
            control = LocalRunControl("request")
            control.bind_runtime_authority({
                "invocation_run_id": "request", "task_id": "task", "run_id": "main",
                "agent_run_id": record["agent_run_id"], "attempt_id": record["attempt_id"],
            })
            batch = prepare_local_task_stop(agent, control)
            assert not batch.unconfirmed
            threading.Thread(target=cleanup_task_resources, args=(batch,), daemon=True).start()
        assert entered.wait(3)
        assert agent.subagents.load(child.id).status == "CANCELLED"
        assert store.load("bg-child").record["stop_requested"]
        # 清理主资源还在等待；新执行在另一线程成功进入 C，证明慢清理没有持控制锁。
        holder = {}

        def resume():
            holder["task"] = agent.subagents.lifecycle.prepare_runner_attempt(child.id)

        thread = threading.Thread(target=resume, daemon=True)
        thread.start()
        thread.join(3)
        assert not thread.is_alive() and "task" in holder
        later = _child(agent, scope, parent_id=child.id)
        _write(store, _resource_scope(scope, holder["task"]), "bg-resumed")
        _write(store, _resource_scope(scope, later), "bg-later")
    finally:
        release.set()
    assert done.wait(3)
    assert set(selected) == {"bg-main", "bg-child"}
    assert not store.load("bg-resumed").record["stop_requested"]
    assert not store.load("bg-later").record["stop_requested"]
    assert agent.subagents.load(later.id).status == "RUNNING"


def test_process_cleanup_runs_after_creation_lock_is_released(main_task, monkeypatch):
    agent, _link, _record, scope, store = main_task
    child = _child(agent, scope)
    _write(store, _resource_scope(scope, child), "bg-child")
    batch = prepare_subagent_stops(agent, [_request(agent.subagents.load(child.id))])
    acquired = threading.Event()

    def clean(_store, frozen):
        def acquire():
            with agent.subagents.creation_guard():
                acquired.set()
        contender = threading.Thread(target=acquire, daemon=True)
        contender.start()
        contender.join(2)
        assert acquired.is_set()
        return ProcessSessionCleanup(frozen, True)

    monkeypatch.setattr(process_resource_stop, "stop_process_session", clean)
    assert cleanup_subagent_stops(batch)["ok"]


@pytest.mark.parametrize("alive,birth,status,exited", [
    (True, "original-birth", "cooperative_requested", False),
    (True, "different-birth", "instance_replaced", True),
    (True, "", "cooperative_requested", False),
    (False, "", "exited", True),
])
def test_host_cleanup_only_observes_original_instance(monkeypatch, alive, birth, status, exited):
    from agent_py_agent.agent.subagents import cancellation_hosts as hosts

    monkeypatch.setattr(hosts, "is_pid_alive", lambda pid: alive)
    monkeypatch.setattr(hosts, "capture_process_birth_token", lambda pid: birth)
    monkeypatch.setattr("os.kill", lambda *args: pytest.fail("观察宿主不能发信号"))
    report = hosts.cleanup_runner_stop(hosts.FrozenRunnerStop({"status": "cooperative_requested"}, 12345, "original-birth"))
    assert report["status"] == status and report["host_exited"] is exited


def test_missing_host_identity_never_authorizes_pid_signal(monkeypatch):
    from agent_py_agent.agent.subagents import cancellation_hosts as hosts

    monkeypatch.setattr(hosts, "has_fresh_runner_session", lambda task: True)
    task = SimpleNamespace(id="child", attributes={"runner_session": {"in_process": False, "worker_pid": 12345}})
    stop = hosts.freeze_runner_stop(task, [task], {"child"}, kill_process=True)
    assert stop.report["status"] == "identity_unavailable" and stop.pid == 0


def test_unconfirmed_cleanup_does_not_claim_success_or_skip_other_resources(main_task, monkeypatch):
    agent, _link, _record, scope, store = main_task
    child = _child(agent, scope)
    grandchild = _child(agent, scope, parent_id=child.id)
    _write(store, _resource_scope(scope, child), "bg-child")
    _write(store, _resource_scope(scope, grandchild), "bg-grandchild")
    batch = prepare_subagent_stops(agent, [_request(agent.subagents.load(child.id))])
    cleaned = []

    def clean(_store, record):
        cleaned.append(record["session_id"])
        return ProcessSessionCleanup(record, record["session_id"] != "bg-child")

    monkeypatch.setattr(process_resource_stop, "stop_process_session", clean)
    result = cleanup_subagent_stops(batch)
    assert not result["ok"]
    assert set(cleaned) == {"bg-child", "bg-grandchild"}
    assert result["failed"][0]["error_code"] == "BACKGROUND_STOP_UNCONFIRMED"


def test_main_pty_failure_keeps_frozen_child_cleanup(main_task, monkeypatch):
    agent, link, _record, scope, store = main_task
    child = _child(agent, scope)
    _write(store, _resource_scope(scope, child), "bg-child")
    monkeypatch.setattr("agent_py_agent.agent.conversation.task_resources.close_main_task_authority", lambda *args, **kwargs: None)

    def request_stop(**kwargs):
        if not kwargs.get("run_id"):
            raise OSError("unmanaged PTY stop unavailable")
        return ()

    monkeypatch.setattr(process_resource_stop.pty_session_registry, "request_stop", request_stop)
    cleaned = threading.Event()

    def clean(_store, record):
        assert record["session_id"] == "bg-child"
        cleaned.set()
        return ProcessSessionCleanup(record, True)

    monkeypatch.setattr(process_resource_stop, "stop_process_session", clean)
    active = _GatewayRequestRecord(None, {
        "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
    }, target_kind="task")
    result = _stop_active_task(agent, active, GatewayControlScope("tester", "chat", "test-session"))
    assert not result.ok and result.error_code == "TASK_RESOURCE_STOP_UNCONFIRMED"
    assert cleaned.wait(3)


def test_main_goal_stop_serializes_with_child_resume_without_lock_inversion(main_task, monkeypatch):
    from contextlib import contextmanager

    agent, link, _record, scope, _store = main_task
    child = _child(agent, scope)
    goals = agent.conversation_store.goals
    goals.create({"thread_id": link.thread_id, "task_id": "task", "objective": "父目标"})
    goals.create({"thread_id": child.agent_thread_id, "task_id": child.id, "objective": "子目标"})
    prepare_subagent_stops(agent, [_request(child, kill_process=False)])
    assert goals.load(child.agent_thread_id, task_id=child.id).status == "paused"
    child_held, main_waiting = threading.Event(), threading.Event()
    errors, results = [], []
    original_guard = agent.subagents.creation_guard

    @contextmanager
    def observed_guard():
        if threading.current_thread() is stopper:
            main_waiting.set()
        with original_guard():
            yield

    def resume_child():
        try:
            with original_guard():
                child_held.set()
                assert main_waiting.wait(3)
                agent.subagents.lifecycle.prepare_runner_attempt(child.id)
                assert goals.load(child.agent_thread_id, task_id=child.id).status == "active"
        except Exception as exc:
            errors.append(exc)

    def stop_main():
        try:
            active = _GatewayRequestRecord(None, {
                "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
            }, target_kind="task")
            results.append(_stop_active_task(agent, active, GatewayControlScope("tester", "chat", "test-session")))
        except Exception as exc:
            errors.append(exc)

    resumer = threading.Thread(target=resume_child, daemon=True)
    stopper = threading.Thread(target=stop_main, daemon=True)
    monkeypatch.setattr(agent.subagents, "creation_guard", observed_guard)
    resumer.start()
    assert child_held.wait(3)
    stopper.start()
    resumer.join(4)
    stopper.join(4)
    assert not resumer.is_alive() and not stopper.is_alive() and not errors
    assert results[0].ok
    assert goals.load(link.thread_id, task_id="task").status == "paused"
    assert goals.load(child.agent_thread_id, task_id=child.id).status == "paused"
    assert agent.subagents.load(child.id).status == "CANCELLED"
