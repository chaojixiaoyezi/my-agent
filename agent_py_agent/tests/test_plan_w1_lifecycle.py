"""W1：只用隔离文件、原工具门和 Event 固定生命周期交错，不重启真实 Gateway。"""
from __future__ import annotations

import os
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import restart_service as service
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.restart_control import execute_restart_control
from agent_py_agent.agent.gateway_parts.status_rendering import gateway_readiness
from agent_py_agent.cli import gateway_process as process
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.cli.models import GatewayRunContext, GatewayThreadsRequest


@pytest.fixture(autouse=True)
def reset_gate():
    service.reset_restart_drain_state()
    yield
    service.reset_restart_drain_state()


def fixture_context(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    config = SimpleNamespace(gateway_request_max_attempts=2, gateway_processing_timeout_seconds=10,
        gateway_user_inflight_limit=2, gateway_global_inflight_limit=4)
    agent = SimpleNamespace(config=config, subagents=SimpleNamespace(workspace=tmp_path / "sub"),
        home_paths=SimpleNamespace(root=tmp_path / "home"))
    identity = {"pid": os.getpid(), "start_time": "original-birth", "host_id": "fixture"}
    context = GatewayRunContext(agent, paths, tmp_path / "agent.yaml", process_identity=identity,
                                process_started_at=time.time())
    monkeypatch.setattr(process, "get_running_pid", lambda _path: None)
    monkeypatch.setattr(process, "recover_gateway_processing_requests", lambda *a, **k: {"requeued": 2, "failed": 0})
    monkeypatch.setattr(process, "log_gateway_event", lambda *a: None)
    monkeypatch.setattr(handover, "log_gateway_event", lambda *a: None)
    return context


def test_setup_never_announces_completion(tmp_path, monkeypatch):
    context = fixture_context(tmp_path, monkeypatch)
    marker = {"request_id": "restart-startup", "old_pid": 123, "drained_at": time.time()}
    monkeypatch.setattr(process, "take_restart_marker", lambda paths: marker)
    completed = []
    monkeypatch.setattr(process, "announce_restart_completed", lambda *a, **k: completed.append(k))
    process._cmd_gateway_run_setup(context.agent, context.paths)
    assert read_json_file(context.paths.state)["status"] == "starting"
    assert completed == [], "starting 不是 ready，不能提前发完成通知"


@pytest.mark.parametrize("bad_field", ["process_identity", "restart_request_id", "missing"])
def test_readiness_requires_exact_generation_and_restart_request(tmp_path, bad_field):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    generation = {"pid": os.getpid(), "start_time": "new-birth"}
    state = {"status": "running", "pid": os.getpid(), "started_at": time.time(),
             "process_identity": generation, "restart_request_id": "restart-current"}
    if bad_field == "missing":
        state.pop("restart_request_id")
    else:
        state[bad_field] = "late-old-generation"
    write_json_file_atomic(paths.state, state)
    # 旧/缺引用的 running 心跳不能绕过严格接班检查。
    write_json_file_atomic(paths.heartbeat, {"pid": os.getpid(), "status": "running"})
    result = gateway_readiness(paths, pid=os.getpid(), process_alive=True,
        expected_generation=generation, restart_request_id="restart-current")
    assert not result.ready
    state.update(process_identity=generation, restart_request_id="restart-current")
    write_json_file_atomic(paths.state, state)
    ready = gateway_readiness(paths, pid=os.getpid(), process_alive=True,
        expected_generation=generation, restart_request_id="restart-current")
    assert ready.ready and ready.ready_observation["restart_request_id"] == "restart-current"


@pytest.mark.parametrize("event_type", [service.CONTINUATION_EVENT_TYPE, service.CANCELLED_EVENT_TYPE])
def test_each_requester_has_notification_result_including_missing_refs(tmp_path, event_type):
    home = tmp_path / "home"
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "admin", "channel": "tui",
        "channel_conversation_id": "restart", "channel_user_id": "admin"})
    requester = {"thread_id": thread.thread_id, "conversation_store_root": str(store.storage.root)}
    record = {"request_id": "restart-notify", "requester": requester,
        "additional_requesters": [{"thread_id": "missing-root"}, {"kind": "cli"}, None,
                                  {**requester, "thread_id": "missing-thread"}]}
    result = service.notify_restart_requesters(record, home_root=home, event_type=event_type, summary="fixture")
    rows = result["notification_results"]
    assert len(rows) == 5 and [row["requester_index"] for row in rows] == list(range(5))
    assert rows[0]["status"] == "written"
    assert [row["reason"] for row in rows[1:]] == [
        "missing_conversation_store_root", "missing_thread_id", "invalid_requester", "thread_not_found"]
    assert result["written"] == 1 and len(result["skipped"]) == 4


def test_control_uses_trusted_store_notification_reference(tmp_path):
    store = ConversationStore(tmp_path / "home" / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "admin", "channel": "tui",
        "channel_conversation_id": "control", "channel_user_id": "admin"})
    paths = gateway_paths_from_root(tmp_path / "gateway")
    admin = SimpleNamespace(conversation_store=store, config=SimpleNamespace(),
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"))
    result = execute_restart_control(admin, thread,
        ConversationControlCommand("restart", value="fixture", operation="apply"), paths=paths)
    assert result.ok
    requester = read_json_file(service.restart_request_path(paths))["requester"]
    assert requester["conversation_store_root"] == str(store.storage.root)
    assert requester["thread_id"] == thread.thread_id


def test_forced_drain_keeps_all_original_operation_refs():
    entered, release = threading.Event(), threading.Event()
    def worker():
        with ExitStack() as stack:
            for index in range(25):
                stack.enter_context(restart_gate.tool_execution_admission({"operation_id": f"op-{index}"}))
            entered.set()
            release.wait(5)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(2)
        result = service.run_restart_drain({"request_id": "forced"}, active_turns=lambda: 0,
            turn_wait_seconds=0, drain_timeout_seconds=.01, force_on_drain_timeout=True)
        assert result["forced_after_drain_timeout"] is True
        refs = result["unresolved_operation_refs"]
        assert {row["operation_id"] for row in refs} == {f"op-{index}" for index in range(25)}
        assert restart_gate.executing_tool_count() == 25
        assert all("success" not in row for row in refs)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize("failed_start", [False, True])
def test_completion_only_after_same_generation_ready(tmp_path, monkeypatch, failed_start):
    context = fixture_context(tmp_path, monkeypatch)
    marker = {"request_id": "restart-startup", "old_pid": 123, "drained_at": time.time()}
    entered, release = threading.Event(), threading.Event()
    completed, errors = [], []
    monkeypatch.setattr(process, "take_restart_marker", lambda paths: marker)
    monkeypatch.setattr(handover, "append_restart_continuations", lambda *a, **k: completed.append(k) or {
        "written": 1, "skipped": [], "notification_results": [{"status": "written"}]})
    def loop(_context, *args):
        stop = args[-1]
        stop.wait(5)
    def start_http(*args):
        entered.set()
        assert release.wait(3)
        if failed_start:
            raise OSError("single controlled bind failure")
        return None
    for name in ("_gateway_heartbeat_loop", "_gateway_request_loop", "_gateway_background_main_loop"):
        monkeypatch.setattr(process, name, loop)
    monkeypatch.setattr(process, "_start_gateway_http", start_http)
    # 真实 setup 必须保留旧二元组返回合同，同时将 marker 显式交给服务发布阶段。
    process._cmd_gateway_run_setup(context.agent, context.paths, context=context, restart_marker=marker)
    request = GatewayThreadsRequest(context, 2, 0, 0)
    resources = []
    def start():
        try:
            resources.extend(process._cmd_gateway_run_threads(request, restart_marker=marker))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=start)
    thread.start()
    try:
        assert entered.wait(2) and completed == []
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        if failed_start:
            assert len(errors) == 1 and completed == []
        else:
            assert errors == [] and len(completed) == 1
            state = read_json_file(context.paths.state)
            assert state["status"] == "running" and state["restart_request_id"] == marker["request_id"]
            assert state["ready_observation"]["generation"] == context.process_identity
    finally:
        release.set()
        if resources:
            resources[0].set()
            for worker in resources[1:4]:
                worker.join(2)
        thread.join(5)


@pytest.mark.parametrize("outcome", ["clean_drain", "forced_after_drain_timeout", "incomplete"])
def test_final_cleanup_decides_handover_and_marker_consumption(tmp_path, monkeypatch, outcome):
    context = fixture_context(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    refs = [{"operation_id": "unconfirmed-op", "run_id": "old-run"}]
    worker = threading.Thread(target=lambda: (entered.set(), release.wait(5)))
    worker.start()
    try:
        assert entered.wait(2)
        forced = outcome == "forced_after_drain_timeout"
        request = service.submit_restart_request(context.paths, target_pid=os.getpid(), requester={"kind": "cli"},
            reason="fixture", cooldown_seconds=0)["request"]
        marker = handover._mark_drained_restart(context, request, {"ok": True,
            "forced_after_drain_timeout": forced, "executing_tools_at_exit": int(forced),
            "executing_tools": refs if forced else [], "unresolved_operation_refs": refs if forced else []})["restart"]
        assert service.consume_restart_marker(context.paths, old_pid_alive=lambda _p: False) is None
        write_json_file_atomic(context.paths.state, {"pid": os.getpid(), "status": "stopped",
            "process_identity": context.process_identity, "started_at": context.process_started_at})
        if outcome == "clean_drain":
            release.set()
            worker.join(2)
        cleanup = {"drain_complete": not worker.is_alive(), "alive_threads": ["request"] if worker.is_alive() else [],
            "background_blockers": [], "unresolved_operation_refs": refs if forced else []}
        result = handover.finalize_restart_cleanup(context, marker, cleanup)
        assert result["status"] == outcome
        assert result["handover_allowed"] is (outcome != "incomplete")
        persisted = read_json_file(service.restart_completed_path(context.paths))
        assert persisted["cleanup_result"]["status"] == outcome
        assert read_json_file(context.paths.state)["cleanup_result"]["status"] == outcome
        consumed = service.consume_restart_marker(context.paths, old_pid_alive=lambda _p: False)
        assert (consumed is not None) is (outcome != "incomplete")
        if forced:
            assert result["unresolved_operation_refs"] == refs
            assert consumed["unresolved_operation_refs"] == refs and worker.is_alive()
        if outcome == "incomplete":
            assert result["notification_results"][0]["reason"] == "missing_thread_id"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


@pytest.mark.parametrize("writer", ["failure", "termination", "drain", "interrupted", "sessions"])
def test_old_generation_late_write_cannot_replace_successor(tmp_path, monkeypatch, writer):
    context = fixture_context(tmp_path, monkeypatch)
    successor = {"pid": os.getpid(), "status": "running", "process_identity": {
        **context.process_identity, "start_time": "successor-birth"}, "started_at": context.process_started_at + 1}
    write_json_file_atomic(context.paths.state, successor)
    if writer == "failure":
        process._record_gateway_run_failed(context.paths, context.agent, os.getpid(), RuntimeError("late"), context=context)
    elif writer == "termination":
        termination = process._GatewayServiceTermination("stopped", "planned_restart", "fixture", "fixture", 0)
        process._record_gateway_service_termination(context.paths, context.agent, os.getpid(), termination, context=context)
    elif writer == "drain":
        process._record_gateway_drain_failure(context.paths, context.agent, os.getpid(), {"alive_threads": ["request"]}, context=context)
    elif writer == "interrupted":
        process._record_gateway_run_interrupted(context.paths, context.agent, os.getpid(), context=context)
    else:
        process._record_surviving_background_sessions_state(context.paths, {"pid": os.getpid(), "surviving_background_sessions": 1}, context=context)
    assert read_json_file(context.paths.state) == successor


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
def test_standalone_entry_never_spawns_after_unexpected_cleanup(tmp_path, monkeypatch, forced, cleanup_error):
    context = fixture_context(tmp_path, monkeypatch)
    context.agent.root = tmp_path
    entered, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=lambda: (entered.set(), release.wait(5)))
    worker.start()
    spawned = []
    monkeypatch.setattr(process, "make_agent", lambda _args: context.agent)
    monkeypatch.setattr(process, "gateway_paths", lambda _agent: context.paths)
    monkeypatch.setattr(process, "_gateway_run_context_from_args", lambda _args: context)
    monkeypatch.setattr(process, "_preflight_required_local_credential", lambda _agent: None)
    monkeypatch.setattr(process, "take_restart_marker", lambda _paths: None)
    monkeypatch.setattr(process, "_install_gateway_signal_handlers", lambda *a: {})
    monkeypatch.setattr(process, "_restore_gateway_signal_handlers", lambda *a: None)
    monkeypatch.setattr(process, "_cmd_gateway_run_threads", lambda *a, **k: (threading.Event(), worker, worker, worker, None))
    def cleanup(request):
        if cleanup_error:
            raise OSError("single controlled cleanup failure")
        return {"drain_complete": False, "alive_threads": ["request"], "background_blockers": [],
                "unresolved_operation_refs": [{"operation_id": "pending"}]}
    monkeypatch.setattr(process, "_cmd_gateway_run_cleanup", cleanup)
    monkeypatch.setattr(process, "hand_over_to_successor", lambda *a, **k: spawned.append(k) or 0)
    def service_loop(_context):
        assert entered.wait(2)
        request = service.submit_restart_request(context.paths, target_pid=os.getpid(), requester={},
            reason="fixture", cooldown_seconds=0)["request"]
        return handover._mark_drained_restart(context, request, {"ok": True,
            "forced_after_drain_timeout": forced, "executing_tools": [{"operation_id": "pending"}],
            "executing_tools_at_exit": 1})
    monkeypatch.setattr(process, "_run_gateway_service_loop", service_loop)
    try:
        result = process._run_standalone_gateway(SimpleNamespace(config=tmp_path / "agent.yaml"), object())
        allowed = forced and not cleanup_error
        assert result == (0 if allowed else 2)
        assert len(spawned) == int(allowed)
        state = read_json_file(context.paths.state)
        assert state["cleanup_result"]["status"] == ("forced_after_drain_timeout" if allowed else "incomplete")
        assert state["status"] == ("stopped" if allowed else "failed")
        if cleanup_error:
            assert state["cleanup_result"]["cleanup_error"] == "OSError"
    finally:
        release.set()
        worker.join(5)


def test_clean_drain_waits_on_original_tool_event():
    entered, drain_started, release = threading.Event(), threading.Event(), threading.Event()
    results = []
    def tool():
        with restart_gate.tool_execution_admission({"operation_id": "clean-op"}):
            entered.set()
            release.wait(5)
    def drain():
        results.append(service.run_restart_drain({"request_id": "clean"}, active_turns=lambda: 0,
            turn_wait_seconds=0, drain_timeout_seconds=2,
            on_progress=lambda facts: drain_started.set() if facts["phase"] == "tool_drain" else None))
    tool_thread = threading.Thread(target=tool)
    drain_thread = threading.Thread(target=drain)
    tool_thread.start()
    try:
        assert entered.wait(2)
        drain_thread.start()
        assert drain_started.wait(2) and results == []
        assert restart_gate.executing_tool_count() == 1 and restart_gate.tool_gate_closed()
        release.set()
        drain_thread.join(3)
        assert results == [{"ok": True, "request_id": "clean", "active_turns_at_exit": 0}]
        assert restart_gate.executing_tool_count() == 0
    finally:
        release.set()
        tool_thread.join(5)
        if drain_thread.ident is not None:
            drain_thread.join(5)
    assert not tool_thread.is_alive() and not drain_thread.is_alive()


def test_cli_restart_accepts_only_requested_successor_ready(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    current = {"pid": 123}
    requested = threading.Event()
    generation = handover.build_process_identity(os.getpid())
    monkeypatch.setattr(handover, "get_running_pid", lambda _path: current["pid"])
    monkeypatch.setattr(handover, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(handover, "refuse_stopping_hosting_gateway", lambda _pid: False)
    submit = service.submit_restart_request
    def schedule(*args, **kwargs):
        result = submit(*args, **kwargs)
        requested.set()
        return result
    monkeypatch.setattr(handover, "submit_restart_request", schedule)
    def successor():
        assert requested.wait(2)
        request_id = read_json_file(service.restart_request_path(paths))["request_id"]
        write_json_file_atomic(paths.state, {"pid": os.getpid(), "status": "running", "started_at": time.time(),
            "process_identity": generation, "restart_request_id": request_id})
        current["pid"] = os.getpid()
    thread = threading.Thread(target=successor)
    thread.start()
    try:
        assert handover.safe_restart_from_cli(SimpleNamespace(config=SimpleNamespace()), paths) == 0
    finally:
        requested.set()
        thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize("mismatch", ["missing", "generation", "request"])
def test_cli_wait_rejects_alive_unbound_successor(tmp_path, monkeypatch, mismatch):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    generation = handover.build_process_identity(os.getpid())
    state = {"pid": os.getpid(), "status": "running", "process_identity": generation, "restart_request_id": "restart"}
    if mismatch == "missing":
        state.pop("restart_request_id")
    elif mismatch == "generation":
        state["process_identity"] = {**generation, "start_time": "foreign"}
    else:
        state["restart_request_id"] = "foreign"
    write_json_file_atomic(paths.state, state)
    monkeypatch.setattr(handover, "get_running_pid", lambda _path: os.getpid())
    assert handover._wait_for_cli_restart(paths, 123, "restart", .01) == 2


def test_failed_successor_keeps_restart_binding_for_shared_readiness(tmp_path, monkeypatch):
    context = fixture_context(tmp_path, monkeypatch)
    write_json_file_atomic(context.paths.state, {"pid": os.getpid(), "status": "running",
        "process_identity": context.process_identity, "started_at": context.process_started_at,
        "restart_request_id": "restart-failed", "cleanup_result": {"status": "clean_drain"}})
    process._record_gateway_run_failed(context.paths, context.agent, os.getpid(), RuntimeError("startup"), context=context)
    result = gateway_readiness(context.paths, pid=os.getpid(), process_alive=True,
        expected_generation=context.process_identity, restart_request_id="restart-failed")
    assert result.state == "failed" and not result.ready


def test_finalizer_rollback_never_overwrites_a_replaced_generation_marker(tmp_path, monkeypatch):
    context = fixture_context(tmp_path, monkeypatch)
    request = service.submit_restart_request(context.paths, target_pid=os.getpid(), requester={}, reason="r", cooldown_seconds=0)["request"]
    marker = handover._mark_drained_restart(context, request, {"ok": True})["restart"]
    successor = {**marker, "old_pid": os.getpid() + 1,
        "old_process_identity": {"pid": os.getpid() + 1, "start_time": "new"}, "cleanup_result": {"status": "pending"}}
    write_json_file_atomic(context.paths.state, {"pid": os.getpid() + 1, "process_identity": successor["old_process_identity"]})
    original = handover.update_json_file_atomic
    replaced = threading.Event()
    def update(path, updater, **kwargs):
        result = original(path, updater, **kwargs)
        if path == service.restart_completed_path(context.paths) and not replaced.is_set():
            replaced.set()
            write_json_file_atomic(path, successor)
        return result
    monkeypatch.setattr(handover, "update_json_file_atomic", update)
    result = handover.finalize_restart_cleanup(context, marker, {"drain_complete": True})
    assert not result["handover_allowed"] and replaced.is_set()
    assert read_json_file(service.restart_completed_path(context.paths)) == successor


def test_consumer_rejects_explicitly_disallowed_handover(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    service.mark_restart_drained(paths, {"request_id": "denied"}, old_pid=123,
        old_process_started_at=1, active_turns_at_exit=0,
        cleanup_result={"status": "clean_drain", "handover_allowed": False})
    assert service.consume_restart_marker(paths, old_pid_alive=lambda _pid: False) is None


def test_state_commit_failure_never_publishes_consumable_marker(tmp_path, monkeypatch):
    context = fixture_context(tmp_path, monkeypatch)
    request = service.submit_restart_request(context.paths, target_pid=os.getpid(), requester={}, reason="r", cooldown_seconds=0)["request"]
    marker = handover._mark_drained_restart(context, request, {"ok": True})["restart"]
    write_json_file_atomic(context.paths.state, {"pid": os.getpid(), "process_identity": context.process_identity,
                                               "started_at": context.process_started_at})
    original = handover.update_json_file_atomic
    entered = threading.Event()
    def update(path, updater, **kwargs):
        if path == context.paths.state:
            entered.set()
            raise OSError("state commit failed")
        return original(path, updater, **kwargs)
    monkeypatch.setattr(handover, "update_json_file_atomic", update)
    with pytest.raises(OSError, match="state commit failed"):
        handover.finalize_restart_cleanup(context, marker, {"drain_complete": True})
    assert entered.is_set()
    assert read_json_file(service.restart_completed_path(context.paths))["cleanup_result"]["status"] == "pending"
    assert service.consume_restart_marker(context.paths, old_pid_alive=lambda _pid: False) is None
