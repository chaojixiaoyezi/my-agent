from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from agent_py_agent.agent.runtime_db.executor_liveness import attempt_executor
from agent_py_agent.agent.subagents.services.executor_recovery import recover_exited_runner
from agent_py_agent.tests.test_dispatch_liveness_and_revive import (
    _closeout_fixture,
    _wakes_for_attempt,
)


def _running(tmp_path):
    manager, store, task, attempt, run, params, _ = _closeout_fixture(tmp_path, owner="test/executor")
    with manager.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='running', ended_at=0 WHERE attempt_id=?", (attempt,))
    return manager, manager.load(task.id), attempt, run


def test_missing_session_dead_process_is_failed_and_notifies_once(tmp_path):
    manager, task, attempt, run = _running(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    with manager.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?",
                     (json.dumps({"runner_pid": child.pid}), attempt))
    result = recover_exited_runner(manager, task)
    assert result["reason"] == "executor_process_died"
    assert manager.load(task.id).status == "FAILED"
    assert manager.runtime_db.agent_run_for_run_id(task.id)["status"] == "failed"
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt)) == 1
    assert recover_exited_runner(manager, manager.load(task.id)) is None
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt)) == 1


def test_slow_live_executor_is_not_reaped_then_return_is_recovered(tmp_path):
    manager, task, attempt, run = _running(tmp_path)
    with attempt_executor(manager.runtime_db, task.id, attempt):
        # 持主持续存在，模型完全没有输出、心跳很旧也不是死亡证据。
        task.heartbeat_at = time.time() - 86400
        manager.save(task)
        assert recover_exited_runner(manager, task) is None
        assert manager.load(task.id).status == "RUNNING"
    result = recover_exited_runner(manager, manager.load(task.id))
    assert result["reason"] == "executor_returned_without_result"
    assert manager.load(task.id).status == "FAILED"


def test_prompt_failure_and_base_exception_have_executor_exit_receipt(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.subagent.params import SubagentRunParams
    from agent_py_agent.agent.agent_core.subagent.run_flow import run_subagent_flow

    manager, task, attempt, run = _running(tmp_path)

    def fail_prompt(*args):
        raise SystemExit("injected executor exit before model")

    lifecycle = SimpleNamespace(
        agent=SimpleNamespace(subagents=manager), prepare_attempt=lambda *a, **kw: attempt,
        probe_channel=lambda *a: None, build_prompt=fail_prompt,
    )
    with pytest.raises(SystemExit):
        run_subagent_flow(lifecycle, SubagentRunParams(run_id=task.id, attempt_id=attempt, dry_run=False))
    assert recover_exited_runner(manager, manager.load(task.id))["status"] == "FAILED"


def test_unknown_tools_remain_blocked_not_replayed_and_notify_parent(tmp_path):
    manager, task, attempt, run = _running(tmp_path)
    with attempt_executor(manager.runtime_db, task.id, attempt):
        with manager.runtime_db.transaction() as conn:
            conn.execute(
                "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
                "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
                "VALUES('effect',?,?,1,1,'run_command','EXECUTING',1,1,1)", (run, attempt),
            )
    result = recover_exited_runner(manager, manager.load(task.id))
    assert result["uncertain_effects"] is True
    assert manager.load(task.id).status == "BLOCKED"
    assert manager.runtime_db.get_attempt(attempt)["status"] == "unknown"
    assert manager.runtime_db.agent_run_for_run_id(task.id)["status"] == "unknown"
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt)) == 1
    with manager.runtime_db._runtime_connection() as conn:
        assert conn.execute("SELECT status FROM tool_operations WHERE operation_id='effect'").fetchone()[0] == "EXECUTING"


def test_live_host_without_executor_identity_is_not_guessed_dead(tmp_path):
    manager, task, attempt, run = _running(tmp_path)
    with manager.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?",
                     (json.dumps({"runner_pid": os.getpid()}), attempt))
    assert recover_exited_runner(manager, task) is None


def test_failed_exit_persistence_keeps_same_process_exit_proof(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db import executor_liveness

    manager, task, attempt, run = _running(tmp_path)
    original = executor_liveness._write_executor

    def write(*args, **kwargs):
        if not kwargs["starting"]:
            raise OSError("injected exit receipt failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(executor_liveness, "_write_executor", write)
    with attempt_executor(manager.runtime_db, task.id, attempt):
        pass
    assert recover_exited_runner(manager, task)["reason"] == "executor_scope_exited"


def test_old_executor_does_not_overwrite_new_attempt(tmp_path):
    manager, task, attempt, run = _running(tmp_path)
    with attempt_executor(manager.runtime_db, task.id, attempt):
        manager.runtime_db.settle_agent_run(agent_run_id=run, attempt_id=attempt, status="failed")
        replacement = manager.runtime_db.create_attempt(run)
    assert replacement["attempt_id"] != attempt
    assert recover_exited_runner(manager, task) is None
    assert manager.runtime_db.get_attempt(replacement["attempt_id"])["status"] == "running"
