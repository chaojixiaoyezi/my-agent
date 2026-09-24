"""真实临时账本与可控清理替身验证停止选择，不作为真实 TUI 验收。"""
from __future__ import annotations

import threading
from dataclasses import asdict, replace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.concurrency.interrupt import is_interrupted, register_interruptible
from agent_py_agent.agent.conversation.background_claim import (
    BackgroundClaimDependencies,
    run_claimed,
)
from agent_py_agent.agent.conversation.task_resources import close_main_task_authority
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _GatewayRequestRecord,
    _stop_active_task,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ManagedOperationStore,
    ToolOperationAuthorityRequest,
)
from agent_py_agent.agent.runtime_db.run_cancellation import RuntimeCancellationConflict
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import process_resource_stop as resources
from agent_py_agent.agent.tooling import process_session_commit as commit_module
from agent_py_agent.agent.tooling.process_scope import ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanup
from agent_py_agent.agent.tooling.process_session_commit import ProcessSessionCommitPendingError
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.tests.test_process_session_store import _reservation


# LLM: 所有账本在 tmp_path；不连接 Gateway、不调用模型，清理测试只能使用替身 PID 记录。
# 函数用途: 建立与正式主链相同的任务/运行归属，让测试穿过实际执行权取消入口。
@pytest.fixture
def main_task(tmp_path):
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), gateway_per_user_owner_scoping=False,
    ), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "tester", "channel": "chat", "channel_conversation_id": "test-session",
        "channel_user_id": "tester",
    })
    link = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task", "goal": "停止测试", "status": "active"})
    repo = agent.subagents.runtime_db
    record = repo.record_run_creation(
        owner_id=agent.home_paths.owner_id, conversation_task_id="task", run_id="main",
        thread_id=thread.thread_id, role="main",
    )
    scope = ProcessExecutionScope(str(agent.home_paths.owner_home_dir), thread.thread_id, "task", "main", record["attempt_id"])
    process_store = ProcessSessionStore(process_session_store_root(scope.owner_home, scope.owner_home))
    return agent, link, record, scope, process_store


def _write(store, scope, session_id):
    return store.write(_reservation(
        session_id, execution_scope=asdict(scope),
        access_scope={"owner_home": scope.owner_home, "owner_id": "tester", "conversation_id": scope.thread_id},
    ))


def _authority(agent, record):
    return ManagedOperationStore(agent.subagents.runtime_db), ToolOperationAuthorityRequest(
        agent.home_paths.owner_id, "main", "task", "operation", "run_command", record["attempt_id"],
    )


def test_main_stop_freezes_only_original_run_and_worker_never_rescans_resume(main_task, monkeypatch):
    agent, link, record, scope, store = main_task
    _write(store, scope, "bg-original")
    _write(store, replace(scope, run_id="child"), "bg-child")
    _write(store, replace(scope, root_task_id="other"), "bg-other-task")
    with agent.conversation_store.tasks.transition_guard("task"):
        selected = close_main_task_authority(
            agent.subagents.runtime_db, owner_home=scope.owner_home,
            task_id="task", thread_id=link.thread_id,
        )
        operations, authority = _authority(agent, record)
        with pytest.raises(AuthorityContextMissing):
            operations.require_authority(authority)
        batch = resources.freeze_process_stop(selected)
    fresh = agent.subagents.runtime_db.create_attempt(record["agent_run_id"])
    _write(store, replace(scope, attempt_id=fresh["attempt_id"]), "bg-resumed")
    consumed = []

    def clean(_store, frozen):
        consumed.append(frozen["session_id"])
        return ProcessSessionCleanup(frozen, True)

    monkeypatch.setattr(resources, "stop_process_session", clean)
    assert resources.cleanup_process_stop(batch)["background_confirmed"]
    assert consumed == ["bg-original"]
    assert store.load("bg-original").record["stop_requested"]
    assert not any(store.load(sid).record["stop_requested"] for sid in ("bg-child", "bg-other-task", "bg-resumed"))


def test_gateway_durable_stop_closes_authority_and_dispatches_main_resources_without_children(main_task, monkeypatch):
    agent, link, record, scope, store = main_task
    _write(store, scope, "bg-main")
    cleaned, observed = threading.Event(), []

    def clean(batch):
        observed.extend(row["session_id"] for row in batch.receipt.records)
        cleaned.set()

    monkeypatch.setattr(resources, "cleanup_process_stop", clean)
    active = _GatewayRequestRecord(None, {
        "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
    }, target_kind="task")
    result = _stop_active_task(agent, active, GatewayControlScope("tester", "chat", "test-session"))
    assert result.ok, result
    assert cleaned.wait(3) and observed == ["bg-main"]
    assert agent.conversation_store.tasks.load("task").status == "interrupted"
    operations, authority = _authority(agent, record)
    with pytest.raises(AuthorityContextMissing):
        operations.require_authority(authority)


def test_stale_published_binding_cannot_close_new_main_attempt(main_task):
    agent, link, record, scope, store = main_task
    _write(store, scope, "bg-original")
    fresh = agent.subagents.runtime_db.create_attempt(record["agent_run_id"])
    binding = {"task_id": "task", "run_id": "main", "agent_run_id": record["agent_run_id"], "attempt_id": record["attempt_id"]}
    with pytest.raises(RuntimeCancellationConflict) as caught:
        close_main_task_authority(
            agent.subagents.runtime_db, owner_home=scope.owner_home,
            task_id="task", thread_id=link.thread_id, binding=binding,
        )
    assert caught.value.reason == "stale_attempt"
    assert agent.subagents.runtime_db.get_attempt(fresh["attempt_id"])["status"] == "running"
    assert not store.load("bg-original").record["stop_requested"]


def test_missing_hot_binding_does_not_borrow_persistent_main(main_task):
    agent, link, record, scope, _store = main_task
    with pytest.raises(RuntimeCancellationConflict) as caught:
        close_main_task_authority(
            agent.subagents.runtime_db, owner_home=scope.owner_home,
            task_id="task", thread_id=link.thread_id, binding={},
        )
    assert caught.value.reason == "main_binding_conflict"
    operations, authority = _authority(agent, record)
    operations.require_authority(authority)


def test_stale_hot_request_never_interrupts_or_closes_resumed_task(main_task):
    agent, link, record, _scope, _store = main_task
    repo = agent.subagents.runtime_db
    fresh = repo.create_attempt(record["agent_run_id"])
    paths = gateway_paths(agent)
    path = paths.processing / "old-request.json"
    payload = {
        "id": "old-request", "kind": "ask", "status": "processing", "user_id": "tester",
        "metadata": {"user_id": "tester", "channel": "chat"},
        "conversation": {"channel": "chat", "channel_user_id": "tester", "channel_conversation_id": "test-session"},
        "execution_attempt_id": "transport",
        "runtime_authority": {
            "schema_version": "gateway_runtime_authority.v1", "request_id": "old-request",
            "gateway_execution_attempt_id": "transport", "task_id": "task", "run_id": "main",
            "agent_run_id": record["agent_run_id"], "attempt_id": record["attempt_id"],
        },
    }
    write_json_file(path, payload)
    active = _GatewayRequestRecord(None, {
        "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
    }, target_kind="task", linked_request=_GatewayRequestRecord(path, payload))
    with register_interruptible("conversation-request:task"):
        result = _stop_active_task(agent, active, GatewayControlScope("tester", "chat", "test-session"))
        assert not result.ok and result.delivery_status == "unknown"
        assert not is_interrupted()
    assert repo.get_attempt(fresh["attempt_id"])["status"] == "running"
    assert agent.conversation_store.tasks.load("task").status == "active"


def test_stopped_old_background_slice_cannot_bind_after_task_becomes_active_again(main_task, monkeypatch):
    agent, link, record, _scope, _store = main_task
    entered, release = threading.Event(), threading.Event()
    results, errors, archives = [], [], []
    params = RunParams(run_id="main", task_id="task", request_id="background-fragment")

    def archive(_agent, bound, _prompt):
        archives.append(bound.attempt_id)
        return bound

    def run_once(_kwargs):
        entered.set()
        assert release.wait(3)
        return runtime_mixin._bind_main_agent_turn_params(agent, "继续当前任务", params)

    monkeypatch.setattr(runtime_mixin, "attach_run_task_workspace_context", archive)
    dependencies = BackgroundClaimDependencies(
        claims=agent.conversation_store.claims,
        claim_scope_id=lambda thread_id, _task_id: thread_id,
        child_owns_task=lambda _task_id: False, terminal_task=lambda _kwargs: False,
        recovery_block=lambda _task_id: None, retire_source=lambda _kwargs: None,
        source_admission=lambda signal: "" if signal is None else "unexpected_source",
        run_once=run_once, runtime_facts=lambda: {}, record_policy_failure=lambda _kwargs: None,
        lease_seconds=10, heartbeat_interval_seconds=1,
    )
    request = {"thread_id": link.thread_id, "task_id": "task", "reason": "test"}

    def execute():
        try:
            results.append(run_claimed(dependencies, request))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert entered.wait(3)
        active = _GatewayRequestRecord(None, {
            "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
        }, target_kind="task")
        stopped = _stop_active_task(agent, active, GatewayControlScope("tester", "chat", "test-session"))
        assert stopped.ok, stopped
        with agent.conversation_store.tasks.transition_guard("task"):
            agent.conversation_store.tasks.update_status({"task_id": "task", "status": "active"})
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and results == [None]
    assert archives == []
    assert len(agent.subagents.runtime_db.attempts_for_run(record["agent_run_id"])) == 1
    fresh = run_claimed(dependencies, request)
    assert fresh is not None and fresh.attempt_id != record["attempt_id"]
    operations, authority = _authority(agent, record)
    operations.require_authority(replace(authority, attempt_id=fresh.attempt_id))


@pytest.mark.parametrize("status,blocked", [("interrupted", True), ("cancelled", True), ("completed", False)])
def test_prebind_stop_check_preserves_completed_notification_admission(main_task, monkeypatch, status, blocked):
    agent, _link, record, _scope, _store = main_task
    agent.conversation_store.tasks.update_status({"task_id": "task", "status": status})
    monkeypatch.setattr(runtime_mixin, "attach_run_task_workspace_context", lambda _a, bound, _p: bound)
    params = RunParams(run_id="main", task_id="task", request_id="notice")
    if blocked:
        with pytest.raises(InterruptedError):
            runtime_mixin._bind_main_agent_turn_params(agent, "处理通知", params)
        assert len(agent.subagents.runtime_db.attempts_for_run(record["agent_run_id"])) == 1
    else:
        bound = runtime_mixin._bind_main_agent_turn_params(agent, "处理通知", params)
        assert bound.attempt_id != record["attempt_id"]


def test_pty_failure_preserves_committed_background_batch_and_gateway_reports_partial(main_task, monkeypatch):
    agent, link, _record, scope, store = main_task
    _write(store, scope, "bg-main")
    agent.conversation_store.goals.create({"thread_id": link.thread_id, "task_id": "task", "objective": "继续检查"})
    observed, dispatched = [], threading.Event()

    def fail_pty(**_kwargs):
        raise RuntimeError("test worker start failure")

    def consume(batch):
        observed.append(batch)
        dispatched.set()

    monkeypatch.setattr(resources.pty_session_registry, "request_stop", fail_pty)
    monkeypatch.setattr(resources, "cleanup_process_stop", consume)
    active = _GatewayRequestRecord(None, {
        "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
    }, target_kind="task")
    result = _stop_active_task(agent, active, GatewayControlScope("tester", "chat", "test-session"))
    assert not result.ok and result.delivery_status == "unknown"
    assert dispatched.wait(3)
    assert observed[0].pty_request_error == "RuntimeError"
    assert [row["session_id"] for row in observed[0].receipt.records] == ["bg-main"]
    assert store.load("bg-main").record["stop_requested"]
    assert agent.conversation_store.goals.load(link.thread_id, task_id="task").status == "paused"


def test_pty_only_request_never_claims_aggregate_exit_confirmation(main_task, monkeypatch):
    _agent, _link, _record, scope, _store = main_task
    monkeypatch.setattr(resources.pty_session_registry, "request_stop", lambda **_kwargs: ("pty-original",))
    report = resources.cleanup_process_stop(resources.freeze_process_stop(scope))
    assert report["background_confirmed"] and report["sessions"] == []
    assert "confirmed" not in report
    assert report["pty"] == {"status": "requested", "session_ids": ("pty-original",), "error_type": ""}


@pytest.mark.parametrize("older_commit", [False, True])
def test_pending_freeze_uses_only_its_own_committed_receipt(main_task, monkeypatch, older_commit):
    _agent, _link, _record, scope, store = main_task
    _write(store, scope, "bg-target")
    other_scope = replace(scope, run_id="other")
    _write(store, other_scope, "bg-unrelated")
    original = commit_module.write_json_file_atomic_unlocked

    def fail_install(path, row):
        if path.name.startswith("bg-"):
            raise OSError("test installation failure")
        return original(path, row)

    with monkeypatch.context() as patch:
        patch.setattr(commit_module, "write_json_file_atomic_unlocked", fail_install)
        if older_commit:
            with pytest.raises(ProcessSessionCommitPendingError):
                store.request_stop(other_scope)
        batch = resources.freeze_process_stop(scope)
        if older_commit:
            assert batch.background_freeze_error == "ProcessSessionCommitPendingError"
            assert not batch.receipt.records and not batch.receipt.transaction_id
            assert not resources.cleanup_process_stop(batch)["background_confirmed"]
        else:
            assert not batch.background_freeze_error
            assert batch.receipt.recovery_required and batch.receipt.committed
            assert [row["session_id"] for row in batch.receipt.records] == ["bg-target"]
    assert store.load("bg-target").record["stop_requested"] is not older_commit
    assert store.load("bg-unrelated").record["stop_requested"] is older_commit


def test_goal_resume_serializes_after_main_stop_freezes_resources(main_task, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.goal_control_service import (
        GoalControlRequest,
        execute_goal_control_operation,
    )

    agent, link, _record, _scope, _store = main_task
    store = agent.conversation_store
    store.goals.create({"thread_id": link.thread_id, "task_id": "task", "objective": "继续检查"})
    control_scope = GatewayControlScope("tester", "chat", "test-session")
    active = _GatewayRequestRecord(None, {
        "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
    }, target_kind="task")
    entered, release, resuming, resumed = (threading.Event() for _ in range(4))
    original = resources.freeze_process_stop
    results, errors = [], []

    def freeze(selected):
        entered.set()
        assert release.wait(3)
        return original(selected)

    def stop():
        try:
            results.append(_stop_active_task(agent, active, control_scope))
        except BaseException as exc:
            errors.append(exc)

    def resume():
        try:
            resuming.set()
            results.append(execute_goal_control_operation(GoalControlRequest(
                agent, store, store.threads.load(link.thread_id), parse_conversation_control("/goal resume"),
                control_scope, lambda _task: None,
            )))
        except BaseException as exc:
            errors.append(exc)
        finally:
            resumed.set()

    monkeypatch.setattr(resources, "freeze_process_stop", freeze)
    stopping, restoring = threading.Thread(target=stop), threading.Thread(target=resume)
    stopping.start()
    try:
        assert entered.wait(3)
        restoring.start()
        assert resuming.wait(3)
        assert not resumed.wait(0.1)
    finally:
        release.set()
        stopping.join(3)
        if restoring.ident is not None:
            restoring.join(3)
    assert not stopping.is_alive() and not restoring.is_alive() and not errors
    assert len(results) == 2 and all(result.ok for result in results)
    assert store.tasks.load("task").status == "active"
    assert store.goals.load(link.thread_id, task_id="task").status == "active"
