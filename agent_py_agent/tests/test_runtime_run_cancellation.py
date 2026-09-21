"""精确执行权取消合同；使用临时真实 RuntimeDB，不运行模型或真实任务。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.runtime_db import repository as repository_module
from agent_py_agent.agent.runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ManagedOperationStore,
    ToolOperationAuthorityRequest,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.run_cancellation import (
    RuntimeCancellationConflict,
    RuntimeCancellationTarget,
    cancel_runtime_run,
)
from agent_py_agent.agent.subagents.cancellation import CancelSubagentTaskRequest, _close_authority


@pytest.fixture
def runtime(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    record = repo.record_run_creation(
        owner_id="owner", conversation_task_id="task", run_id="main", role="main", attempt_status="pending",
    )
    activated = repo.create_attempt(record["agent_run_id"], reuse_pending=True)
    assert activated["attempt_id"] == record["attempt_id"]
    target = RuntimeCancellationTarget("task", "main", record["agent_run_id"], record["attempt_id"])
    return repo, target


def _cancel(repo, target, **kwargs):
    return cancel_runtime_run(repo, target, reason="conversation_user_stop", source="test", **kwargs)


def _authority(target):
    return ToolOperationAuthorityRequest(
        owner_id="owner", task_id=target.task_id, run_id=target.run_id,
        attempt_id=target.attempt_id, operation_id="background", tool_name="run_command",
    )


def _locks(repo):
    with repo._runtime_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM resource_locks ORDER BY lock_id")]


def test_cancel_blocks_original_tool_authority_but_explicit_new_attempt_can_run(runtime):
    repo, target = runtime
    operations = ManagedOperationStore(repo)
    operations.require_authority(_authority(target))
    report = _cancel(repo, target)
    assert report["authority_closed"] and report["attempt_id"] == target.attempt_id
    with pytest.raises(AuthorityContextMissing):
        operations.require_authority(_authority(target))
    new = repo.create_attempt(target.agent_run_id)
    operations.require_authority(_authority(replace(target, attempt_id=new["attempt_id"])))
    with pytest.raises(RuntimeCancellationConflict) as caught:
        _cancel(repo, target)
    assert caught.value.reason == "stale_attempt"
    assert repo.get_attempt(new["attempt_id"])["status"] == "running"


@pytest.mark.parametrize("field,value", [
    ("task_id", "other"), ("run_id", "other"), ("agent_run_id", "other"),
    ("attempt_id", "other"), ("attempt_id", ""),
])
def test_identity_mismatch_never_closes_authority(runtime, field, value):
    repo, target = runtime
    before = _locks(repo)
    assert before and {row["attempt_id"] for row in before} == {target.attempt_id}
    with pytest.raises(RuntimeCancellationConflict):
        _cancel(repo, replace(target, **{field: value}))
    assert _locks(repo) == before
    ManagedOperationStore(repo).require_authority(_authority(target))


@pytest.mark.parametrize("late_unknown", [False, True])
def test_unknown_is_preserved_with_its_lock_and_recovery_barrier(runtime, monkeypatch, late_unknown):
    repo, target = runtime
    before = _locks(repo)
    assert before and {row["attempt_id"] for row in before} == {target.attempt_id}

    def mark_unknown():
        assert repo._mark_attempt_unknown(
            target.attempt_id, target.agent_run_id, reason="unconfirmed effect", operator="test",
        )

    if late_unknown:
        original = repo.settle_agent_run

        def settle(**kwargs):
            mark_unknown()
            return original(**kwargs)

        monkeypatch.setattr(repo, "settle_agent_run", settle)
    else:
        mark_unknown()
    report = _cancel(repo, target)
    assert report["status"] == "unknown_preserved" and report["authority_closed"]
    assert repo.get_attempt(target.attempt_id)["status"] == "unknown"
    assert repo.get_agent_run(target.agent_run_id)["status"] == "created"
    assert _locks(repo) == before
    assert repo.main_agent_recovery_block_for_task("task") is not None
    with pytest.raises(AuthorityContextMissing):
        ManagedOperationStore(repo).require_authority(_authority(target))


def test_crash_reconciled_run_and_attempt_unknown_preserve_real_execution_lock(runtime, monkeypatch):
    repo, target = runtime
    before = _locks(repo)
    assert before and before[0]["attempt_id"] == target.attempt_id
    monkeypatch.setattr(repository_module, "_proc_state", lambda _pid: "dead")
    assert repo.recover_stale_attempts() == [target.agent_run_id]
    assert repo.get_agent_run(target.agent_run_id)["status"] == "unknown"
    report = _cancel(repo, target)
    assert report["status"] == "unknown_preserved"
    assert repo.get_agent_run(target.agent_run_id)["status"] == "unknown"
    assert _locks(repo) == before
    assert repo.main_agent_recovery_block_for_task("task") is not None


def test_unrecognized_run_status_is_not_treated_as_known_unknown(runtime):
    repo, target = runtime
    assert repo._mark_attempt_unknown(target.attempt_id, target.agent_run_id, reason="uncertain", operator="test")
    with repo.transaction() as conn:
        conn.execute("UPDATE agent_runs SET status = 'unrecognized' WHERE agent_run_id = ?", (target.agent_run_id,))
    with pytest.raises(RuntimeCancellationConflict) as caught:
        _cancel(repo, target)
    assert caught.value.reason == "unknown_status"
    assert _locks(repo)


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_already_terminal_reports_original_status_without_rewriting_history(runtime, status):
    repo, target = runtime
    repo.settle_agent_run(agent_run_id=target.agent_run_id, attempt_id=target.attempt_id, status=status)
    before = dict(repo.get_attempt(target.attempt_id))
    report = _cancel(repo, target)
    assert report["status"] == status and report["replayed"]
    assert dict(repo.get_attempt(target.attempt_id)) == before


@pytest.mark.parametrize("activate_before_commit", [False, True])
def test_pending_replacement_is_checked_at_database_commit(tmp_path, monkeypatch, activate_before_commit):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    record = repo.record_run_creation(
        owner_id="owner", conversation_task_id="task", run_id="child", attempt_status="pending",
    )
    context = SimpleNamespace(attempt_id="old-projection", now=None, reason="stop", source="test")
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    task = SimpleNamespace(id="child", runner_active_attempt_id="old-projection")
    if activate_before_commit:
        original = repo.settle_agent_run

        def settle(**kwargs):
            assert kwargs["expected_attempt_status"] == "pending"
            activated = repo.create_attempt(record["agent_run_id"], reuse_pending=True)
            assert activated["attempt_id"] == record["attempt_id"]
            return original(**kwargs)

        monkeypatch.setattr(repo, "settle_agent_run", settle)
        with pytest.raises(RuntimeCancellationConflict) as caught:
            _close_authority(agent.subagents, CancelSubagentTaskRequest(task, context.reason, source=context.source))
        assert caught.value.reason == "attempt_status_conflict"
        assert repo.get_attempt(record["attempt_id"])["status"] == "running"
        assert _locks(repo)
    else:
        report = _close_authority(agent.subagents, CancelSubagentTaskRequest(task, context.reason, source=context.source))
        assert report["attempt_id"] == record["attempt_id"]
        assert report["status"] == "cancelled"


def test_expected_status_requires_explicit_attempt(runtime):
    repo, target = runtime
    result = repo.settle_agent_run(
        agent_run_id=target.agent_run_id, status="cancelled", expected_attempt_status="pending",
    )
    assert result == {"settled": False, "reason": "missing_attempt"}
    ManagedOperationStore(repo).require_authority(_authority(target))


@pytest.mark.parametrize("runtime_status,projection_status", [("done", "DONE"), ("failed", "FAILED"), ("unknown", "ABANDONED")])
def test_full_child_cancel_does_not_overwrite_terminal_or_unknown_as_cancelled(tmp_path, runtime_status, projection_status):
    from agent_py_agent.agent.agent_core.orchestration.tools.cancel import execute_cancel_subagents
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(goal="停止边界", thought="临时合同", plan=["检查状态"])
    repo = agent.subagents.runtime_db
    run = repo.agent_run_for_run_id(task.id)
    attempt = repo.create_attempt(run["agent_run_id"], reuse_pending=True)
    if runtime_status == "unknown":
        assert repo._mark_attempt_unknown(attempt["attempt_id"], run["agent_run_id"], reason="uncertain", operator="test")
    else:
        repo.settle_agent_run(agent_run_id=run["agent_run_id"], attempt_id=attempt["attempt_id"], status=runtime_status)
    task.status = projection_status
    task.runner_active_attempt_id = ""
    agent.subagents.save(task)
    before = dict(repo.get_attempt(attempt["attempt_id"]))
    locks = _locks(repo)
    result = execute_cancel_subagents(agent, {"run_id": task.id, "kill_process": False, "reason": "停止任务"})
    assert result.ok
    assert '"cancel_status": "preserved"' in result.output
    loaded = agent.subagents.load(task.id)
    assert loaded.status == projection_status and "cancel_subagents" not in loaded.attributes
    assert dict(repo.get_attempt(attempt["attempt_id"])) == before
    assert _locks(repo) == locks
