from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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


def test_unknown_effects_keep_the_host_failure_type_for_the_parent(tmp_path):
    """宿主显式给的 executor_effects_unknown 必须原样留在任务与父级唤醒里（2026-10-02 C4 真实核对发现）。
    改前它不在 FailureType 枚举里，被结果状态改写成可自动重跑族里的通用 runner_error，父级分不出“效果未知、要先核对”。"""
    manager, task, attempt, run = _running(tmp_path)
    with attempt_executor(manager.runtime_db, task.id, attempt), manager.runtime_db.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES('effect',?,?,1,1,'write_file','EXECUTING',1,1,1)", (run, attempt),
        )

    recover_exited_runner(manager, manager.load(task.id))

    assert manager.load(task.id).failure_type == "executor_effects_unknown"
    [(_path, wake)] = _wakes_for_attempt(tmp_path, task.id, attempt)
    assert wake["metadata"]["failure_type"] == "executor_effects_unknown"


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


# 函数用途: 让 _running 准备好的那个执行区间停在本进程里（按需记一条未确认的工具效果、按需打停机记号），再把它的执行器身份
#   改成一个已死的进程，模拟“网关停机、进程退出把还在跑的子代理带走、重启后收尾”。返回放行线程的函数。
def _executor_taken_by_process_exit(running, *, marked: bool, uncertain: bool):
    from agent_py_agent.agent.runtime_db.executor_liveness import (
        mark_in_process_executors_host_shutdown,
    )

    manager, task, attempt, _run = running
    entered, release = threading.Event(), threading.Event()

    def executor():
        with attempt_executor(manager.runtime_db, task.id, attempt):
            entered.set()
            assert release.wait(10)

    worker = threading.Thread(target=executor, daemon=True)
    worker.start()
    assert entered.wait(5)
    if uncertain:
        with manager.runtime_db.transaction() as conn:
            conn.execute(
                "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
                "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
                "VALUES('effect',?,?,1,1,'run_command','EXECUTING',1,1,1)",
                (manager.runtime_db.agent_run_for_run_id(task.id)["agent_run_id"], attempt),
            )
    if marked:
        assert mark_in_process_executors_host_shutdown() >= 1
        assert recover_exited_runner(manager, manager.load(task.id)) is None, "执行器还活着，打了记号也不判死"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    with manager.runtime_db.transaction() as conn:
        metadata = json.loads(conn.execute("SELECT metadata_json FROM agent_attempts WHERE attempt_id=?",
                                           (attempt,)).fetchone()[0])
        metadata["executor"] = {**metadata["executor"], "pid": child.pid, "process_epoch": "previous-gateway-process"}
        conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(metadata), attempt))

    # 函数用途: 放行停在执行区间里的线程（模拟进程早已退出之后，收尾不再受它影响）。
    def finish():
        release.set()
        worker.join(5)

    return finish


@pytest.mark.parametrize(("marked", "uncertain", "expected"), [
    (True, False, "host_shutdown_interrupted"),
    (False, False, "runner_error"),
    (True, True, "executor_effects_unknown"),
], ids=["host-shutdown", "no-mark", "unknown-effects-first"])
def test_executor_taken_by_host_shutdown_is_closed_out_as_host_shutdown(tmp_path, marked, uncertain, expected):
    """step17c 停机预演观察 1（2026-10-02）：自然停机时网关进程里的子代理随进程消失，重启后被收尾成 runner_error
    （“执行器已退出但没有返回结果”），看不出是停机。现在 Gateway 关门结清后给本进程在跑的执行器打记号，重启收尾按
    host_shutdown_interrupted 收口，不自动重跑；没有记号（kill -9 等）照旧 runner_error；有未确认工具效果时仍先核对。"""
    from agent_py_agent.agent.subagents.models import RETRYABLE_RUNNER_FAILURE_TYPES
    from agent_py_agent.agent.subagents.runner_display_projection import runner_display_label

    running = _running(tmp_path)
    manager, task, attempt, _run = running
    finish = _executor_taken_by_process_exit(running, marked=marked, uncertain=uncertain)
    try:
        result = recover_exited_runner(manager, manager.load(task.id))
    finally:
        finish()

    assert (result["reason"], result["host_shutdown"]) == ("executor_process_died", marked)
    assert manager.load(task.id).failure_type == expected
    [(_path, wake)] = _wakes_for_attempt(tmp_path, task.id, attempt)
    assert wake["metadata"]["failure_type"] == expected
    if expected == "host_shutdown_interrupted":
        assert manager.load(task.id).status == "FAILED" and expected not in RETRYABLE_RUNNER_FAILURE_TYPES
        assert runner_display_label("FAILED", expected) == "宿主停机中断"


def test_host_shutdown_mark_only_touches_its_own_running_executor(tmp_path):
    """停机记号按 token CAS：执行器记录已换成别的执行器（token 不同）时不写，只认自己那条仍在运行的记录。"""
    from agent_py_agent.agent.runtime_db.executor_liveness import (
        mark_in_process_executors_host_shutdown,
    )

    manager, task, attempt, run = _running(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def executor():
        with attempt_executor(manager.runtime_db, task.id, attempt):
            entered.set()
            assert release.wait(10)

    worker = threading.Thread(target=executor, daemon=True)
    worker.start()
    assert entered.wait(5)
    try:
        with manager.runtime_db.transaction() as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM agent_attempts WHERE attempt_id=?",
                                               (attempt,)).fetchone()[0])
            metadata["executor"] = {**metadata["executor"], "token": "another-executor"}
            conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(metadata), attempt))
        mark_in_process_executors_host_shutdown()
        with manager.runtime_db._runtime_connection() as conn:
            stored = json.loads(conn.execute("SELECT metadata_json FROM agent_attempts WHERE attempt_id=?",
                                             (attempt,)).fetchone()[0])
        assert "host_shutdown" not in stored["executor"]
    finally:
        release.set()
        worker.join(5)
