"""W4：使用真实canonical事务验证过期申请与授权交错。"""
import gc

import pytest

from agent_py_agent.agent.subagents.capability_request_expiry import close_stale_capability_requests
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
from agent_py_agent.tests.test_gateway_unpromoted_stop import hot as hot


def _blocked(hot, *, parent="task", goal="exact duty"):
    manager = hot.agent.subagents
    task = manager.create_run(goal=goal, parent_id=parent, root_id=parent, depth=1, role="worker")
    request = manager.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem="need permission", needed_capability="filesystem", requested_tools=["write_file"],
    ))

    def mark(current):
        current.status = "BLOCKED"
        current.failure_type = "capability_request"
        current.runner_active_attempt_id = ""

    manager.mutate(task.id, mark)
    return manager.load(task.id), request


def test_parent_interruption_closes_open_and_drops_blocked_priority(hot):
    from agent_py_agent.agent.conversation.agent_activity import (
        _subagent_sort_key,
        subagent_display_label,
    )

    task, request = _blocked(hot)
    manager = hot.agent.subagents
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_interrupted") == [task.id]
    closed = manager.load(task.id)
    assert closed.capability_requests[0].status == "CLOSED"
    assert closed.attributes["capability_request_closeout"]["resolution"]["reason"] == "parent_turn_interrupted"
    assert closed.capability_requests[0].constraints["resolution_reason"] == "parent_turn_interrupted"
    assert closed.status == "CANCELLED" and closed.ended_at > 0
    assert subagent_display_label(closed) == "已过期"
    assert _subagent_sort_key(closed)[0] > _subagent_sort_key(task)[0]
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_ended") == []
    assert manager.load(task.id).capability_requests[0].id == request.id


def test_granted_request_is_not_expired(hot):
    task, _ = _blocked(hot)
    manager = hot.agent.subagents
    manager.mutate(task.id, lambda current: setattr(current.capability_requests[0], "status", "GRANTED"))
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_ended") == []
    assert manager.load(task.id).capability_requests[0].status == "GRANTED"


def test_replacement_requires_exact_parent_role_goal(hot):
    task, _ = _blocked(hot)
    manager = hot.agent.subagents
    other, _ = _blocked(hot, goal="not same duty")
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="replaced", replaced_by_run_id=other.id) == []
    replacement = manager.create_run(goal=task.goal, parent_id="task", root_id="task", depth=1, role=task.role)
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="replaced", replaced_by_run_id=replacement.id) == [task.id]
    resolution = manager.load(task.id).attributes["capability_request_closeout"]["resolution"]
    assert resolution == {"reason": "replaced", "replaced_by_run_id": replacement.id}
    assert manager.load(other.id).capability_requests[0].status == "OPEN"


def test_closeout_exact_snapshot_does_not_touch_later_child(hot):
    old, _ = _blocked(hot)
    newer, _ = _blocked(hot)
    closed = close_stale_capability_requests(hot.agent.subagents, parent_task_id="task", reason="user_denied", run_ids=(old.id,))
    assert closed == [old.id]
    assert hot.agent.subagents.load(newer.id).capability_requests[0].status == "OPEN"


def test_invalid_closeout_reason_is_rejected(hot):
    with pytest.raises(ValueError):
        close_stale_capability_requests(hot.agent.subagents, parent_task_id="task", reason="guessed")


def test_confirmed_interrupt_closes_parent_requests(hot):
    from agent_py_agent.agent.gateway_parts import control_service

    task, _ = _blocked(hot)
    active = control_service._GatewayRequestRecord(None, {
        "id": "task", "conversation_thread_id": hot.scope.thread_id,
    }, "task")
    result = control_service._turn_interrupted_result(hot.agent, active, hot.control_scope)
    assert result.ok
    assert hot.agent.subagents.load(task.id).capability_requests[0].status == "CLOSED"


def test_committed_parent_terminal_closes_only_frozen_children(hot, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.tests.test_v13_w4_conversation_steer_stop import (
        _promote_same_request_without_backfill,
    )

    _promote_same_request_without_backfill(hot)
    old, _ = _blocked(hot, parent="message")
    archive = worker.archive_request
    later = []

    def interleaved(*args, **kwargs):
        later.append(_blocked(hot, parent="message")[0])
        return archive(*args, **kwargs)

    monkeypatch.setattr(worker, "archive_request", interleaved)
    worker._finish_claimed_gateway_request(hot.paths, hot.path, "message", {
        "id": "message", "status": "done", "ok": True, "response": "done",
    }, conversation_store=hot.agent.conversation_store, execution_agent=hot.agent)
    assert (hot.paths.terminal / "message.json").exists()
    assert hot.agent.subagents.load(old.id).capability_requests[0].status == "CLOSED"
    assert hot.agent.subagents.load(later[0].id).capability_requests[0].status == "OPEN"


@pytest.mark.parametrize("grant_first", [True, False])
def test_grant_and_expiry_are_serialized_by_canonical_transaction(hot, grant_first):
    import threading

    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityGrantParams

    manager = hot.agent.subagents
    task, request = _blocked(hot)
    first_committed, release = threading.Event(), threading.Event()
    errors = []

    def grant():
        manager.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(
            request_id=request.id, grant_type="tool", tools=["write_file"], reason="explicit parent grant",
        ))

    def expire():
        return close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_ended")

    def first():
        try:
            (grant if grant_first else expire)()
            first_committed.set()
            assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)
            first_committed.set()

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert first_committed.wait(10)
        assert not errors
        if grant_first:
            assert expire() == []
        else:
            with pytest.raises(ValueError):
                grant()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    current = manager.load(task.id)
    assert current.capability_requests[0].status == ("GRANTED" if grant_first else "CLOSED")
    assert not current.runner_active_attempt_id


def test_explicit_interrupt_snapshot_preserves_new_request_on_same_child(hot, monkeypatch):
    from agent_py_agent.agent.conversation import agent_activity
    from agent_py_agent.agent.gateway_parts import control_service

    task, old = _blocked(hot)
    close = agent_activity.close_parent_capability_requests
    newer = []

    def interleaved(agent, parent, **kwargs):
        newer.append(agent.subagents.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
            problem="new duty", needed_capability="filesystem", requested_tools=["edit_file"])))
        return close(agent, parent, **kwargs)

    monkeypatch.setattr(agent_activity, "close_parent_capability_requests", interleaved)
    active = control_service._GatewayRequestRecord(None, {
        "id": "task", "conversation_thread_id": hot.scope.thread_id}, "task")
    assert control_service._turn_interrupted_result(hot.agent, active, hot.control_scope).ok
    current = hot.agent.subagents.load(task.id)
    assert {request.id: request.status for request in current.capability_requests} == {
        old.id: "CLOSED", newer[0].id: "OPEN"}
    assert current.status == "BLOCKED"


def test_historical_grant_does_not_keep_closed_current_duty_blocked(hot):
    task, old = _blocked(hot)
    manager = hot.agent.subagents
    manager.mutate(task.id, lambda current: setattr(current.capability_requests[0], "status", "GRANTED"))
    newer = manager.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem="new duty", needed_capability="filesystem", requested_tools=["edit_file"]))
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_ended",
        run_ids=(task.id,), request_ids={task.id: (newer.id,)}) == [task.id]
    current = manager.load(task.id)
    assert current.status == "CANCELLED"
    assert {request.id: request.status for request in current.capability_requests} == {
        old.id: "GRANTED", newer.id: "CLOSED"}


@pytest.mark.parametrize("concurrent", [False, True])
def test_count0_real_stop_and_completion_finish_without_lock_cycle(request, tmp_path, monkeypatch, concurrent):
    """隔离子进程跑真实停止/完成；仅在原文件锁获取前用Event固定交错。"""
    import os
    import subprocess
    import sys
    import threading

    from agent_py_agent.agent.gateway_parts import control_service, request_worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.subagents import manager as managers
    from agent_py_agent.tests.test_v13_w4_conversation_steer_stop import (
        _promote_same_request_without_backfill,
    )

    if os.environ.get("R1_COUNT0_LOCK_CHILD") != "1":
        result = subprocess.run(
            [sys.executable, "-m", "pytest", request.node.nodeid, "-q", "--tb=short", "-o", "addopts=", "-p", "no:cacheprovider"],
            env={**os.environ, "R1_COUNT0_LOCK_CHILD": "1"}, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    hot = request.getfixturevalue("hot")
    record = _promote_same_request_without_backfill(hot)
    child, _ = _blocked(hot, parent="message")
    stop_has_cg, finish_at_cg = threading.Event(), threading.Event()
    original_guard = managers.subagent_creation_guard
    original_fresh = control_service._fresh_stop_runtime_binding
    errors, stopped = [], []
    from contextlib import contextmanager

    @contextmanager
    def scheduled_guard(workspace):
        if threading.current_thread().name == "r1-finish":
            finish_at_cg.set()
        with original_guard(workspace):
            yield

    def scheduled_fresh(*args):
        stop_has_cg.set()
        assert finish_at_cg.wait(3)
        return original_fresh(*args)

    def stop():
        try:
            stopped.append(control_service._stop_live_window_request(
                hot.agent, hot.paths, control_service._GatewayRequestRecord(hot.path, hot.payload), hot.control_scope))
        except BaseException as error:
            errors.append(error)

    def finish():
        try:
            if concurrent:
                assert stop_has_cg.wait(3)
            request_worker._finish_claimed_gateway_request(hot.paths, hot.path, "message",
                {"id": "message", "status": "cancelled", "ok": False, "response": "已停止"},
                conversation_store=hot.agent.conversation_store, execution_agent=hot.agent)
        except BaseException as error:
            errors.append(error)

    from agent_py_agent.tests.test_rework_v14_admission_io_recovery import fd_count

    gc.collect()
    before = fd_count()
    if concurrent:
        monkeypatch.setattr(managers, "subagent_creation_guard", scheduled_guard)
        monkeypatch.setattr(control_service, "_fresh_stop_runtime_binding", scheduled_fresh)
        jobs = [threading.Thread(target=stop, name="r1-stop", daemon=True),
                threading.Thread(target=finish, name="r1-finish", daemon=True)]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join(5)
        assert not any(job.is_alive() for job in jobs), "Count0 CG/T completion deadline exceeded"
    else:
        stop()
        finish()
    assert not errors, errors
    assert stopped and stopped[0].ok
    terminal = hot.paths.terminal / "message.json"
    canonical = read_json_file(terminal)
    assert canonical["status"] == "cancelled" and canonical["capability_closeout"]["acknowledged"]
    assert hot.repo.get_attempt(record["attempt_id"])["status"] == "cancelled"
    assert hot.agent.subagents.load(child.id).capability_requests[0].status == "CLOSED"
    thread = hot.agent.conversation_store.threads.require(hot.scope.thread_id)
    assert not thread.active_task_ids
    prior = terminal.read_bytes()
    finish()
    assert terminal.read_bytes() == prior
    # 增长才是泄漏；负差可来自既存描述符正常关闭（CI曾观察到-1）。
    gc.collect()
    assert fd_count() - before <= 0


def _fd_snapshot():
    """记录FD号和内核文件身份；不是open-file-description的出生身份。"""
    import errno
    import os
    import sys

    result = set()
    for name in os.listdir('/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'):
        fd = int(name)
        try:
            info = os.fstat(fd)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue  # listdir自身的临时目录FD已关闭，不属于可观察的存活集合。
        result.add((fd, info.st_dev, info.st_ino, info.st_mode, info.st_rdev))
    return result


def _wait_for_fd_convergence(original_fds, *, timeout=5.0):
    """给后台扫描和迭代器回收有限收尾时间；超时仍返回真实存活FD供严格断言。"""
    import time

    deadline = time.monotonic() + timeout
    while True:
        gc.collect()
        current = _fd_snapshot()
        remaining = deadline - time.monotonic()
        if current <= original_fds or remaining <= 0:
            return current
        time.sleep(min(0.01, remaining))


def test_fd_convergence_waits_for_background_directory_scan(tmp_path, monkeypatch):
    import os
    import threading

    gc.collect()
    before = _fd_snapshot()
    ready, release = threading.Event(), threading.Event()
    snapshots = []
    snapshot = _fd_snapshot

    def scan():
        with os.scandir(tmp_path):
            ready.set()
            assert release.wait(5)

    def observe():
        current = snapshot()
        snapshots.append(current)
        release.set()
        return current

    worker = threading.Thread(target=scan)
    worker.start()
    try:
        assert ready.wait(5)
        monkeypatch.setitem(globals(), '_fd_snapshot', observe)
        after = _wait_for_fd_convergence(before)
        assert not snapshots[0] <= before, '第一次快照必须仍观察到扫描目录FD'
        assert len(snapshots) >= 2 and after <= before
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_fd_convergence_keeps_persistent_leaks_visible(tmp_path, kind):
    import os

    gc.collect()
    before = _fd_snapshot()
    resource = os.scandir(tmp_path) if kind == 'directory' else (tmp_path / 'leaked').open('wb')
    with resource:
        assert not _wait_for_fd_convergence(before, timeout=0) <= before
    assert _wait_for_fd_convergence(before) <= before


@pytest.mark.parametrize('new_owned_path', [None, 'new-owned', 'inherited'])
def test_fd_snapshot_distinguishes_inherited_cleanup_from_masked_leak(tmp_path, new_owned_path):
    with (tmp_path / 'inherited').open('wb') as inherited:
        before = _fd_snapshot()
        inherited_fd = inherited.fileno()
    assert inherited.closed
    # 已明确关闭的句柄没有继续存活资格，即使同号重开同文件也不算旧资源。
    allowed = {identity for identity in before if identity[0] != inherited_fd}
    if new_owned_path:
        with (tmp_path / new_owned_path).open('wb'):
            assert not _fd_snapshot() <= allowed, '关闭旧FD不能掩盖同号新FD'
    else:
        assert _fd_snapshot() <= allowed, '仅关闭继承FD不应误报泄漏'


def test_headless_stream_steer_stop_new_restart_next_conversation(tmp_path, monkeypatch):
    """原claim/worker/core/control/stream往返；仅外部模型用有界Event后端，非真实供应商验收。"""
    import json
    import threading
    from dataclasses import replace
    from uuid import uuid4

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import control_service, recovery, request_worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.gateway_parts.request_execution import claimed_request_chunk_path
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), gateway_per_user_owner_scoping=False)
    agent = SimpleAgent(config, tmp_path)
    # 有Home的运行按原模型目录选档，不把部署echo配置误当正式选择；离线档不含网络或凭据。
    profile_id = str(uuid4())
    execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "transport": "offline", "model_backend": "echo", "model_name": "headless-local",
        "model_context_window_tokens": 200_000,
    }})
    execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    scope = control_service.GatewayControlScope("tester", "chat", "sequence")
    conversation = {"channel": "chat", "channel_conversation_id": "sequence", "channel_user_id": "tester", "canonical_user_id": "tester"}
    thread = agent.conversation_store.threads.get_or_create(conversation)
    streaming, release = threading.Event(), threading.Event()
    errors, completed, calls = [], [], []
    def generate(backend, prompt, on_chunk=None, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            assert on_chunk is not None
            on_chunk("正在生成原始响应。" * 100)
            streaming.set()
            assert release.wait(10), "test provider wait must remain bounded"
            return ModelResponse(text="停止前的迟到结果", backend=backend.name)
        return ModelResponse(text="下一轮的完整回答", backend=backend.name)

    def enqueue(rid):
        path = paths.inbox / (rid + ".json")
        write_json_file_atomic(path, {"id": rid, "kind": "ask", "goal": "验证连续交互", "status": "pending", "verbose_level": "stream",
            "user_id": "tester", "metadata": {"channel": "chat", "user_id": "tester"}, "conversation": conversation})
        return path

    # 先经同一worker完成本地echo请求，让SQLite、运行时及其惰性单例初始化落在FD基线之前。
    warmup = enqueue("sequence-warmup")
    assert request_worker._process_gateway_request_path(agent, paths, warmup, "sequence-warmup")
    assert read_json_file(paths.responses / warmup.name)["ok"]
    assert not agent.conversation_store.threads.require(thread.thread_id).active_task_ids
    monkeypatch.setattr(EchoBackend, "generate", generate)
    first = enqueue("sequence-first")
    # 排除随后明确关闭的继承FD，不能用旧FD的关闭抵消新资源泄漏。
    with (tmp_path / 'inherited-fd').open('wb') as inherited:
        gc.collect()
        original_fds = {identity for identity in _fd_snapshot() if identity[0] != inherited.fileno()}

    def worker():
        try:
            completed.append(request_worker._process_gateway_request_path(agent, paths, first, "sequence-worker"))
        except BaseException as error:
            errors.append(error)
        finally:
            # 早退也唤醒断言，保留真实答复，避免被误诊成提供方流超时。
            streaming.set()

    running = threading.Thread(target=worker, daemon=True)
    running.start()
    try:
        assert streaming.wait(10) and not errors, errors
        assert calls, (completed, read_json_file(paths.responses / first.name), read_json_file(paths.processing / first.name))
        chunk = claimed_request_chunk_path(paths.processing / first.name, first.stem)
        events = [json.loads(line) for line in chunk.read_text().splitlines()]
        deltas = [row for row in events if row.get("kind") == "assistant_text_delta"]
        assert deltas and deltas[0]["model_call_id"] and deltas[0]["seq"] == 0
        assert "正在生成原始响应。" in "".join(row["text"] for row in deltas)
        # 原活跃claim拒绝第二执行体，不以线程数量冒充scope并发数。
        assert not agent.conversation_store.claims.acquire({"thread_id": thread.thread_id, "task_id": "another-active"})
        steer_scope = replace(scope, metadata={"message_id": "sequence-steer", "expected_turn_id": first.stem})
        steer = control_service.steer_active_conversation_if_running(agent, paths, message="补充要求", scope=steer_scope)
        assert steer is not None and steer.ok and steer.request_id == first.stem
        stopped = control_service.execute_gateway_conversation_control(agent, paths, parse_conversation_control("/stop"), scope)
        assert stopped.ok, stopped
    finally:
        release.set()
        running.join(10)
    assert not running.is_alive() and not errors and completed == [True], errors
    terminal = paths.terminal / first.name
    saved = terminal.read_bytes()
    canonical = read_json_file(terminal)
    assert canonical["status"] in {"cancelled", "interrupted"}
    assert canonical["capability_closeout"]["acknowledged"]
    assert not agent.conversation_store.threads.require(thread.thread_id).active_task_ids
    reset = control_service.execute_gateway_conversation_control(agent, paths, parse_conversation_control("/new"), scope)
    assert reset.ok, reset
    # 重建真实Agent重读同一home，并经原恢复入口修复投影，不重放原模型工作。
    restarted = SimpleAgent(config, tmp_path)
    report = recovery.recover_gateway_processing_requests_report(paths, params=recovery._RecoveryContext(
        __import__("time").time(), 3, 10, False, restarted, None))
    assert not report.load_errors
    repaired = recovery.repair_gateway_terminal_projections(paths, agent=restarted, full_scan=True)
    assert not repaired[1]
    assert terminal.read_bytes() == saved and len(calls) == 1
    next_path = enqueue("sequence-next")
    assert request_worker._process_gateway_request_path(restarted, paths, next_path, "sequence-successor")
    answer = read_json_file(paths.responses / next_path.name)
    assert answer["ok"] and answer["response"] == "下一轮的完整回答"
    assert len(calls) == 2 and not tuple(paths.processing.glob("*.json"))
    assert not restarted.conversation_store.threads.require(thread.thread_id).active_task_ids
    assert terminal.read_bytes() == saved
    # GC及后台扫描有有限收尾时间；只用收敛后的同一快照判决，不再另采一次含瞬态目录FD的总数。
    # 身份集合仍严格零新增，持续存活的文件或目录FD不能被1-FD宽限掩盖。
    after_fds = _wait_for_fd_convergence(original_fds)
    assert after_fds <= original_fds, ('new live descriptors', after_fds - original_fds)
