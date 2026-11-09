"""独立 Python 宿主沿原心跳接收取消；仅开发替身，未调用模型或真实 TUI。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner import session_pool, worker
from agent_py_agent.agent.agent_core.runner.session_pool import RunnerSessionPoolLease
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.subagents.cancellation import (
    CancelSubagentTaskRequest,
    prepare_subagent_stops,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.runner_control import runner_attempt_cancelled


@pytest.fixture
def running(tmp_path):
    manager = SubAgentManager(tmp_path / "runs", owner_home_dir=str(tmp_path / "owner"))
    tasks = [manager.create_run(goal=f"受控执行 {index}", thought="开发验证", plan=["等待控制"]) for index in range(2)]
    tasks = [manager.lifecycle.prepare_runner_attempt(task.id) for task in tasks]
    return manager, tasks


# LLM: 只等待本测试子进程的结构化标记，超时保留输出并失败；清理仅使用本次 Popen 句柄。
# 函数用途: 确认原 worker 已登记或退出，不靠固定长睡眠猜测执行进度。
def _wait_marker(process, marker):
    deadline = time.monotonic() + 5
    while not marker.exists():
        assert process.poll() is None, process.stderr.read()
        assert time.monotonic() < deadline, f"未等到 {marker.name}"
        time.sleep(0.01)


@pytest.fixture
def ready_stop_control(monkeypatch):
    """只在原读事务证明pending拒绝且尚未广播时续作，不代替产品的未知门。"""
    from agent_py_agent.agent.subagents import cancellation
    from agent_py_agent.agent.subagents.services.persistence.run_index import (
        SubagentRunIndex,
        SubagentRunIndexUnavailable,
    )

    signals, refusals = [], []
    denied = threading.Event()
    signal = cancellation.signal_runner_attempt
    require_complete = SubagentRunIndex._require_complete

    def observed_signal(run_id, attempt_id):
        signals.append((run_id, attempt_id))
        return signal(run_id, attempt_id)

    def observed_complete(index, db):
        try:
            return require_complete(index, db)
        except SubagentRunIndexUnavailable as error:
            # request_ids已BEGIN；同一快照内采样，不能拿后来的就绪状态猜原错误原因。
            error.stop_test_index_state = {
                'complete': db.execute("SELECT value FROM meta WHERE key='complete'").fetchone() == (1,),
                'pending': db.execute('SELECT 1 FROM runs WHERE pending=1 LIMIT 1').fetchone() is not None,
            }
            refusals.append(error.stop_test_index_state)
            denied.set()
            raise

    monkeypatch.setattr(cancellation, 'signal_runner_attempt', observed_signal)
    monkeypatch.setattr(SubagentRunIndex, '_require_complete', observed_complete)

    def prepare(agent, requests):
        deadline = time.monotonic() + 5
        while True:
            signal_count = len(signals)
            try:
                return prepare_subagent_stops(agent, requests)  # 默认include_descendants保持原协议。
            except SubagentRunIndexUnavailable as error:
                if (getattr(error, 'stop_test_index_state', None) != {'complete': True, 'pending': True}
                        or len(signals) != signal_count):
                    raise
                while True:
                    if time.monotonic() >= deadline:
                        raise
                    state = agent.subagents.persistence.run_index.readiness()
                    if state['complete'] is not True:
                        raise
                    if state['pending'] is False:
                        break
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    raise

    return SimpleNamespace(prepare=prepare, signals=signals, refusals=refusals, denied=denied)


@pytest.mark.parametrize("resume_before_observation", [False, True])
def test_separate_host_relays_exact_cancel_without_interrupting_sibling(
    tmp_path, running, resume_before_observation, ready_stop_control,
):
    manager, tasks = running
    script = '''
import sys, threading
from pathlib import Path
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.agent_core.runner.session_pool import RunnerSessionPoolLease, runner_session_lease
from agent_py_agent.agent.concurrency.interrupt import register_interruptible, wait_interruptibly
root = Path(sys.argv[1])
manager = SubAgentManager(root / "runs", owner_home_dir=str(root / "owner"))
def run(index, run_id, attempt_id):
    with runner_session_lease(RunnerSessionPoolLease(manager, run_id, interval_seconds=0.2, attempt_id=attempt_id)):
        with register_interruptible(f"subagent-runner-attempt:{run_id}:{attempt_id}"):
            (root / f"ready-{index}").write_text(attempt_id)
            try:
                wait_interruptibly(20)
            except InterruptedError:
                (root / f"stopped-{index}").write_text(attempt_id)
            else:
                raise AssertionError("未收到停止")
threads = [threading.Thread(target=run, args=(i, sys.argv[2 + i * 2], sys.argv[3 + i * 2])) for i in range(2)]
for thread in threads: thread.start()
for thread in threads: thread.join()
'''
    args = [value for task in tasks for value in (task.id, task.runner_active_attempt_id)]
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    agent = SimpleNamespace(subagents=manager, conversation_store=None)
    try:
        _wait_marker(process, tmp_path / "ready-0")
        _wait_marker(process, tmp_path / "ready-1")
        batch = ready_stop_control.prepare(agent, [CancelSubagentTaskRequest(
            manager.load(tasks[0].id), "conversation_user_stop", kill_process=False,
        )])
        assert not batch.unconfirmed and batch.stops[0].report["thread_interrupt"] == "not_found"
        if resume_before_observation:
            resumed = manager.lifecycle.prepare_runner_attempt(tasks[0].id)
            assert resumed.runner_active_attempt_id != tasks[0].runner_active_attempt_id
        _wait_marker(process, tmp_path / "stopped-0")
        assert process.poll() is None and not (tmp_path / "stopped-1").exists()
        assert manager.load(tasks[1].id).runner_active_attempt_id == tasks[1].runner_active_attempt_id
        if resume_before_observation:
            assert manager.load(tasks[0].id).runner_active_attempt_id == resumed.runner_active_attempt_id
        ready_stop_control.prepare(agent, [CancelSubagentTaskRequest(manager.load(tasks[1].id), "test", kill_process=False)])
        _wait_marker(process, tmp_path / "stopped-1")
        process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("status", ["done", "failed"])
def test_normal_terminal_result_does_not_interrupt_its_own_commit(running, status):
    manager, tasks = running
    task = tasks[0]
    row = manager.runtime_db.agent_run_for_run_id(task.id)
    manager.runtime_db.settle_agent_run(agent_run_id=row["agent_run_id"], attempt_id=task.runner_active_attempt_id, status=status)
    task.status = status.upper()
    task.runner_active_attempt_id = ""
    manager.save(task)
    assert runner_attempt_cancelled(manager, task.id, tasks[1].runner_active_attempt_id)
    original = manager.runtime_db.current_attempt(row["agent_run_id"])["attempt_id"]
    assert not runner_attempt_cancelled(manager, task.id, original)


def test_temporary_control_read_failure_does_not_interrupt_a_healthy_worker(running, monkeypatch):
    manager, tasks = running
    task = tasks[0]
    monkeypatch.setattr(manager.runtime_db, "agent_run_for_run_id", lambda run_id: (_ for _ in ()).throw(OSError("read")))
    monkeypatch.setattr("agent_py_agent.agent.concurrency.interrupt.interrupt_by_name", lambda name: pytest.fail("不能误停"))
    assert not session_pool._relay_runner_stop(RunnerSessionPoolLease(manager, task.id, attempt_id=task.runner_active_attempt_id))


def test_stop_before_thread_registration_cannot_enter_model(running):
    manager, tasks = running
    task = tasks[0]
    prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(task, "test", kill_process=False)])
    stub = SimpleNamespace(subagents=manager, run_subagent=lambda **kwargs: pytest.fail("停止后不能运行模型"))
    with pytest.raises(InterruptedError):
        worker._run_subagent_worker_interruptibly(stub, SimpleNamespace(run_id=task.id), attempt_id=task.runner_active_attempt_id)


def test_late_overlay_cannot_write_to_resumed_attempt(running):
    manager, tasks = running
    original = tasks[0]
    original.runtime_identity = SimpleNamespace(config_overlay_ref="overlay.json", config_scope="run")
    agent = SimpleNamespace(subagents=manager, config=SimpleNamespace())
    prepare_subagent_stops(agent, [CancelSubagentTaskRequest(manager.load(original.id), "conversation_user_stop", kill_process=False)])
    resumed = manager.lifecycle.prepare_runner_attempt(original.id)
    with pytest.raises(RuntimeConflictError):
        worker._record_effective_config_overlay(agent, original.id, original, attempt_id=original.runner_active_attempt_id)
    current = manager.load(original.id)
    assert current.runner_active_attempt_id == resumed.runner_active_attempt_id
    assert "runtime_config_overlay" not in current.attributes


def test_cancel_between_control_read_and_heartbeat_write_is_still_relayed(running, monkeypatch):
    from agent_py_agent.agent.concurrency.interrupt import (
        interrupt_by_name,
        register_interruptible,
        wait_interruptibly,
    )

    manager, tasks = running
    task = tasks[0]
    token = f"subagent-runner-attempt:{task.id}:{task.runner_active_attempt_id}"
    original_write = session_pool._record_runner_session
    cancellation_committed, interrupted = threading.Event(), threading.Event()
    failures = []
    monkeypatch.setattr("agent_py_agent.agent.subagents.cancellation.signal_runner_attempt", lambda *_: "not_found")

    def write_after_foreign_stop(lease, session, *, status, **kwargs):
        if status == "running" and not kwargs.get("require_persisted") and not cancellation_committed.is_set():
            batch = prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(
                manager.load(task.id), "foreign_stop", kill_process=False,
            )])
            assert not batch.unconfirmed
            cancellation_committed.set()
        return original_write(lease, session, status=status, **kwargs)

    monkeypatch.setattr(session_pool, "_record_runner_session", write_after_foreign_stop)

    def run():
        try:
            with session_pool.runner_session_lease(RunnerSessionPoolLease(
                manager, task.id, interval_seconds=0.2, attempt_id=task.runner_active_attempt_id,
            )), register_interruptible(token):
                try:
                    wait_interruptibly(10)
                except InterruptedError:
                    interrupted.set()
        except Exception as exc:
            failures.append(exc)

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    try:
        assert cancellation_committed.wait(3)
        assert interrupted.wait(3), "心跳写入被停止拒绝后，仍须转交原轮取消"
        assert not failures
    finally:
        interrupt_by_name(token)
        runner.join(3)
    assert not runner.is_alive()


def test_pending_sibling_commit_refuses_before_broadcast_and_retries_exact_attempt(running, ready_stop_control, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service

    manager, tasks = running
    original_prepare = manager.persistence.prepare_run_index
    original_write = service.write_agent_run_state
    marked, failures = threading.Event(), []

    def pause_before_canonical(state):
        if threading.current_thread() is writer:
            marked.set()  # mark_pending已耐久提交，但canonical写及upsert尚未发生。
            assert ready_stop_control.denied.wait(5)
        return original_write(state)

    def save_sibling():
        try:
            manager.save(tasks[0])
        except BaseException as error:
            failures.append(error)

    writer = threading.Thread(target=save_sibling)
    started = False
    def prepare_then_start_pending():
        nonlocal started
        result = original_prepare()
        if not started:
            started = True
            writer.start()
            assert marked.wait(5)
        return result

    monkeypatch.setattr(service, 'write_agent_run_state', pause_before_canonical)
    monkeypatch.setattr(manager.persistence, 'prepare_run_index', prepare_then_start_pending)
    try:
        request = CancelSubagentTaskRequest(tasks[1], 'test', kill_process=False)
        batch = ready_stop_control.prepare(SimpleNamespace(subagents=manager), [request])
        assert not batch.unconfirmed
        assert ready_stop_control.refusals == [{'complete': True, 'pending': True}]
        assert ready_stop_control.signals == [(tasks[1].id, tasks[1].runner_active_attempt_id)]
        assert runner_attempt_cancelled(manager, tasks[1].id, tasks[1].runner_active_attempt_id)
        assert not runner_attempt_cancelled(manager, tasks[0].id, tasks[0].runner_active_attempt_id)
        assert manager.load(tasks[0].id).runner_active_attempt_id == tasks[0].runner_active_attempt_id
    finally:
        ready_stop_control.denied.set()
        if started:
            writer.join(5)
    assert not writer.is_alive() and failures == []


def test_pending_index_error_after_broadcast_is_never_retried(running, ready_stop_control, monkeypatch):
    from agent_py_agent.agent.subagents import cancellation
    from agent_py_agent.agent.subagents.services.persistence.run_index import (
        SubagentRunIndexUnavailable,
    )

    manager, tasks = running
    calls = []
    original_errors = []

    def fail_after_signal(agent, requests):
        calls.append(requests)
        request = requests[0]
        cancellation.signal_runner_attempt(request.task.id, request.task.runner_active_attempt_id)
        index = manager.persistence.run_index
        index.mark_pending(tasks[0].id, task=tasks[0])
        with index.connect() as db:
            db.execute('BEGIN')
            try:
                index._require_complete(db)
            except SubagentRunIndexUnavailable as error:
                original_errors.append(error)
                raise
        pytest.fail('真实pending必须拒绝')

    monkeypatch.setattr(sys.modules[__name__], 'prepare_subagent_stops', fail_after_signal)
    with pytest.raises(SubagentRunIndexUnavailable) as caught:
        ready_stop_control.prepare(SimpleNamespace(subagents=manager), [
            CancelSubagentTaskRequest(tasks[1], 'test', kill_process=False),
        ])
    assert len(calls) == 1 and original_errors == [caught.value]
    assert ready_stop_control.signals == [(tasks[1].id, tasks[1].runner_active_attempt_id)]
    assert ready_stop_control.refusals == [{'complete': True, 'pending': True}]
