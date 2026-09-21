"""原创建锁覆盖持久入口；只用临时 Store、受控线程与子进程，不替代 TUI 验收。"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    _requeue_dead_running,
)
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    _queue_resolved_child_continuation,
)
from agent_py_agent.agent.subagents.coordination import subagent_creation_guard
from agent_py_agent.agent.subagents.manager import SubAgentManager


# LLM: helper 捕获后台异常并有界 join；daemon 只避免失败夹具挂住 pytest，不证明产品线程可以遗弃。
# 函数用途: 启动一个可检查完成与异常的测试线程，确保并发失败不会被静默吞掉。
def _start(action, *, name=None):
    result = queue.Queue()
    entered = threading.Event()

    def run():
        entered.set()
        try:
            result.put((True, action()))
        except BaseException as exc:
            result.put((False, exc))

    thread = threading.Thread(target=run, daemon=True, name=name)
    thread.start()
    assert entered.wait(2)
    return thread, result


def _finish(job):
    thread, results = job
    thread.join(5)
    assert not thread.is_alive()
    ok, result = results.get_nowait()
    if not ok:
        raise result
    return result


def _manager(tmp_path):
    return SubAgentManager(tmp_path / "runs", owner_home_dir=str(tmp_path / "owner"))


def test_nested_guard_uses_same_path_across_managers_and_releases_on_error(tmp_path):
    first = _manager(tmp_path)
    second = SubAgentManager(tmp_path / "runs" / ".", owner_home_dir=str(tmp_path / "owner"))
    acquired = threading.Event()

    def competitor():
        with second.creation_guard():
            acquired.set()

    with pytest.raises(ValueError, match="leave"):
        with first.creation_guard():
            with second.creation_guard():
                job = _start(competitor)
                assert not acquired.wait(0.05)
                raise ValueError("leave")
    _finish(job)
    assert acquired.is_set()


def test_different_owner_guard_is_independent(tmp_path):
    with subagent_creation_guard(tmp_path / "first"):
        job = _start(lambda: _create_under_guard(tmp_path / "second"))
        assert _finish(job) == "entered"


def _create_under_guard(workspace):
    with subagent_creation_guard(workspace):
        return "entered"


@pytest.mark.skipif(sys.platform == "win32", reason="现有创建文件锁未提供 Windows OS 互斥")
def test_creation_guard_serializes_a_separate_python_process(tmp_path):
    script = (
        "import sys; from agent_py_agent.agent.subagents.coordination import subagent_creation_guard; "
        "print('waiting', flush=True)\n"
        "with subagent_creation_guard(sys.argv[1]): print('entered', flush=True)\n"
    )
    with subagent_creation_guard(tmp_path):
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            ready = _start(process.stdout.readline)
            assert _finish(ready).strip() == "waiting"
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.1)
        except BaseException:
            process.kill()
            process.wait(timeout=3)
            raise
    try:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "entered"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.parametrize("operation", ["create", "prepare", "abandon"])
def test_canonical_mutation_waits_for_original_creation_guard(tmp_path, operation):
    manager = _manager(tmp_path)
    task = manager.create_run(goal="执行轮协调")
    active = manager.lifecycle.prepare_runner_attempt(task.id)
    actions = {
        "create": lambda: manager.create_run(goal="创建另一个孩子"),
        "prepare": lambda: manager.lifecycle.prepare_runner_attempt(task.id),
        "abandon": lambda: manager.lifecycle.abandon_runner_attempt(task.id, active.runner_active_attempt_id),
    }
    with manager.creation_guard():
        job = _start(actions[operation])
        with pytest.raises(queue.Empty):
            job[1].get(timeout=0.05)
        if operation == "prepare":
            current = manager.load(task.id)
            current.status = "CANCELLED"
            manager.save(current)
    if operation == "prepare":
        with pytest.raises(RuntimeError, match="terminal run"):
            _finish(job)
    else:
        _finish(job)


def test_late_abandon_preserves_new_attempt_and_concurrent_fields(tmp_path):
    manager = _manager(tmp_path)
    task = manager.create_run(goal="旧轮放弃不能清新轮")
    first = manager.lifecycle.prepare_runner_attempt(task.id)
    repo = manager.runtime_db
    run = repo.agent_run_for_run_id(task.id)
    repo.settle_agent_attempt(agent_run_id=run["agent_run_id"], attempt_id=first.runner_active_attempt_id)
    second = manager.lifecycle.prepare_runner_attempt(task.id)
    manager.mutate(task.id, lambda current: current.attributes.update({"concurrent_fact": "preserved"}))
    result = manager.lifecycle.abandon_runner_attempt(task.id, first.runner_active_attempt_id)
    assert result.runner_active_attempt_id == second.runner_active_attempt_id
    assert result.status == "RUNNING"
    assert result.attributes["concurrent_fact"] == "preserved"
    assert first.runner_active_attempt_id in result.runner_abandoned_attempt_ids
    assert repo.agent_run_for_run_id(task.id)["current_attempt_id"] == second.runner_active_attempt_id


def test_abandon_read_cannot_straddle_new_attempt_publication(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    task = manager.create_run(goal="放弃与换轮交错")
    first = manager.lifecycle.prepare_runner_attempt(task.id)
    repo = manager.runtime_db
    run = repo.agent_run_for_run_id(task.id)
    repo.settle_agent_attempt(agent_run_id=run["agent_run_id"], attempt_id=first.runner_active_attempt_id)
    captured, release = threading.Event(), threading.Event()
    original_load = manager.persistence.load

    def delayed_read(run_id):
        value = original_load(run_id)
        if threading.current_thread().name == "old-abandon" and not captured.is_set():
            captured.set()
            assert release.wait(3)
        return value

    monkeypatch.setattr(manager.persistence, "load", delayed_read)
    abandon = _start(
        lambda: manager.lifecycle.abandon_runner_attempt(task.id, first.runner_active_attempt_id),
        name="old-abandon",
    )
    prepare = None
    try:
        assert captured.wait(2)
        prepare = _start(lambda: manager.lifecycle.prepare_runner_attempt(task.id))
        with pytest.raises(queue.Empty):
            prepare[1].get(timeout=0.05)
        other = _manager(tmp_path)
        other.mutate(task.id, lambda current: current.attributes.update({"concurrent_fact": "preserved"}))
    finally:
        release.set()
    abandoned = _finish(abandon)
    second = _finish(prepare)
    assert abandoned.attributes["concurrent_fact"] == "preserved"
    loaded = manager.load(task.id)
    assert loaded.status == "RUNNING"
    assert loaded.runner_active_attempt_id == second.runner_active_attempt_id
    assert loaded.runner_active_attempt_id != first.runner_active_attempt_id
    assert loaded.attributes["concurrent_fact"] == "preserved"


def test_capability_queue_reloads_control_terminal_after_acquiring_guard(tmp_path):
    manager = _manager(tmp_path)
    stale = manager.create_run(goal="停止后旧授权不能重排")
    agent = SimpleNamespace(subagents=manager)
    with manager.creation_guard():
        job = _start(lambda: _queue_resolved_child_continuation(
            agent, stale, decision="allow", resolved=[{"request_id": "request-a"}], errors=[],
        ))
        with pytest.raises(queue.Empty):
            job[1].get(timeout=0.05)
        current = manager.load(stale.id)
        current.status = "CANCELLED"
        manager.save(current)
    assert _finish(job)["reason"] == "run_closed_by_control"
    assert manager.load(stale.id).status == "CANCELLED"


def test_old_supervision_snapshot_does_not_requeue_changed_runner_session(tmp_path):
    manager = _manager(tmp_path)
    task = manager.create_run(goal="巡检不能覆盖新活动")
    stale = manager.lifecycle.prepare_runner_attempt(task.id)
    current = manager.load(task.id)
    current.attributes["runner_session"] = {"session_id": "new-session", "status": "running"}
    manager.save(current)
    assert not _requeue_dead_running(manager, stale, task.id, reason="process_missing")
    loaded = manager.load(task.id)
    assert loaded.status == "RUNNING"
    assert loaded.runner_active_attempt_id == stale.runner_active_attempt_id
