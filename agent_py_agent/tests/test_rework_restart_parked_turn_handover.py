"""OPUS-R4-P1-03：计划内安全重启时，停在关闭工具关口前的前台回合交给接班进程续跑，不否决交接。

真实 drain_for_requested_restart、_cmd_gateway_run_cleanup 与 finalize_restart_cleanup 由 _run_standalone_gateway
串起；前台回合持真实 request_worker.admission，并停在真实 restart_gate 工具关口前。只替换进程、信号、心跳与接班
spawn 等外部动作，等待全部有上界。负对照：收尾时仍在执行的工具、后台阻塞、存活循环线程与 cleanup 异常照旧否决
非强制交接；普通 stop 带在途前台回合仍按排空未完成退出 2。
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.gateway_parts import restart_service
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_worker import admission
from agent_py_agent.cli import gateway_process as process
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.cli.models import GatewayRunContext

# 线程与屏障等待上界（秒），只作失败兜底；正常路径由 Event 立即唤醒。
_WAIT = 5
_CONTINUATION = {"reason": "foreground_turn_unsettled", "ref": "gateway_admission", "count": 1}


@pytest.fixture(autouse=True)
def reset_gate():
    restart_service.reset_restart_drain_state()
    yield
    restart_service.reset_restart_drain_state()


def _context(tmp_path, monkeypatch, *, force=True):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    config = SimpleNamespace(gateway_request_max_attempts=2, gateway_processing_timeout_seconds=10,
        gateway_user_inflight_limit=2, gateway_global_inflight_limit=4,
        gateway_restart_turn_wait_seconds=0.3, gateway_restart_drain_timeout_seconds=1,
        gateway_restart_force_on_drain_timeout=force)
    agent = SimpleNamespace(config=config, root=tmp_path, subagents=SimpleNamespace(workspace=tmp_path / "sub"),
        home_paths=SimpleNamespace(root=tmp_path / "home"))
    identity = {"pid": os.getpid(), "start_time": "original-birth", "host_id": "fixture"}
    context = GatewayRunContext(agent, paths, tmp_path / "agent.yaml", process_identity=identity,
                                process_started_at=time.time())
    monkeypatch.setattr(process, "get_running_pid", lambda _path: None)
    monkeypatch.setattr(process, "recover_gateway_processing_requests", lambda *a, **k: {"requeued": 0, "failed": 0})
    monkeypatch.setattr(process, "log_gateway_event", lambda *a: None)
    monkeypatch.setattr(handover, "log_gateway_event", lambda *a: None)
    return context


def _standalone(context, monkeypatch, service_loop):
    """桩掉外部动作后跑真实 _run_standalone_gateway；三条循环在 stop_event 后真实退出。返回 (退出码, 接班记录)。"""
    spawned = []
    stop_event = threading.Event()
    loops = [threading.Thread(target=stop_event.wait, args=(_WAIT,)) for _ in range(3)]

    def run_threads(*_args, **_kwargs):
        for loop in loops:
            loop.start()
        return (stop_event, *loops, None)

    monkeypatch.setattr(process, "make_agent", lambda _args: context.agent)
    monkeypatch.setattr(process, "gateway_paths", lambda _agent: context.paths)
    monkeypatch.setattr(process, "_gateway_run_context_from_args", lambda _args: context)
    monkeypatch.setattr(process, "_preflight_required_local_credential", lambda _agent: None)
    monkeypatch.setattr(process, "take_restart_marker", lambda _paths: None)
    monkeypatch.setattr(process, "_install_gateway_signal_handlers", lambda *a: {})
    monkeypatch.setattr(process, "_restore_gateway_signal_handlers", lambda *a: None)
    monkeypatch.setattr(process, "remove_pid_file_if_owned", lambda *a: None)
    monkeypatch.setattr(process, "remove_gateway_stop_request_if_owned", lambda *a, **k: None)
    monkeypatch.setattr(process, "_write_gateway_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(process, "_cmd_gateway_run_threads", run_threads)
    monkeypatch.setattr(process, "hand_over_to_successor", lambda *a, **k: spawned.append(k) or 0)
    monkeypatch.setattr(process, "_run_gateway_service_loop", service_loop)
    try:
        code = process._run_standalone_gateway(SimpleNamespace(config=context.config_path), object())
    finally:
        stop_event.set()
        for loop in loops:
            if loop.ident is not None:
                loop.join(_WAIT)
    return code, spawned


class _ParkedTurn:
    """前台回合：持真实 admission，等工具关口关闭后进入下一工具准入；领到执行权后停到 release。"""

    def __init__(self):
        self.started, self.parking, self.executing, self.release = (threading.Event() for _ in range(4))
        self.admitted = []
        self.thread = threading.Thread(target=self._run)

    def _run(self):
        assert admission.try_acquire_report("user-a", 2, 4) == ""
        try:
            self.started.set()
            deadline = time.monotonic() + _WAIT
            while not restart_gate.tool_gate_closed() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.parking.set()
            with restart_gate.tool_execution_admission({"operation_id": "next-tool"}) as admitted:
                self.admitted.append(admitted)
                self.executing.set()
                self.release.wait(_WAIT)
        finally:
            admission.release("user-a")


def _restart_service_loop(turn, *, reopen_gate=False):
    """服务循环替身：前台回合在途后提交重启并跑真实两段排空；reopen_gate 模拟排空后意外重开关口。"""
    def service_loop(context):
        assert turn.started.wait(_WAIT)
        restart_service.submit_restart_request(context.paths, target_pid=os.getpid(), requester={"kind": "cli"},
            reason="fixture", cooldown_seconds=0)
        report = handover.drain_for_requested_restart(context, dict(context.process_identity))
        assert turn.parking.wait(_WAIT)
        if reopen_gate:
            # 停着的回合领到下一工具执行权，收尾时工具仍在执行。
            restart_gate.open_tool_gate()
            assert turn.executing.wait(_WAIT)
        return report
    return service_loop


def test_planned_restart_hands_parked_foreground_turn_to_successor(tmp_path, monkeypatch):
    context = _context(tmp_path, monkeypatch, force=True)
    turn = _ParkedTurn()
    turn.thread.start()
    try:
        code, spawned = _standalone(context, monkeypatch, _restart_service_loop(turn))
        assert code == 0
        assert len(spawned) == 1
        assert turn.thread.is_alive() and turn.admitted == [] and restart_gate.executing_tool_count() == 0
        state = read_json_file(context.paths.state)
        assert state["status"] == "stopped"
        assert (state.get("restart_drain") or {}).get("phase") != "cancelled"
        marker = read_json_file(restart_service.restart_completed_path(context.paths))
        for cleanup_result in (state["cleanup_result"], marker["cleanup_result"]):
            assert cleanup_result["status"] == "clean_drain"
            assert cleanup_result["handover_allowed"] is True
            assert cleanup_result["unresolved_operation_refs"] == []
            assert cleanup_result["continuation_refs"] == [_CONTINUATION]
        assert marker["active_turns_at_exit"] == 1
        assert restart_service.consume_restart_marker(context.paths, old_pid_alive=lambda _p: False) == marker
    finally:
        restart_service.reset_restart_drain_state()
        turn.release.set()
        turn.thread.join(_WAIT)
    assert not turn.thread.is_alive() and turn.admitted == [True]


def test_tool_still_executing_at_cleanup_blocks_unforced_handover(tmp_path, monkeypatch):
    context = _context(tmp_path, monkeypatch, force=False)
    turn = _ParkedTurn()
    turn.thread.start()
    try:
        code, spawned = _standalone(context, monkeypatch, _restart_service_loop(turn, reopen_gate=True))
        assert code == 2
        assert spawned == []
        state = read_json_file(context.paths.state)
        assert state["status"] == "failed"
        assert state["restart_drain"]["phase"] == "cancelled"
        cleanup_result = state["cleanup_result"]
        assert cleanup_result["status"] == "incomplete" and cleanup_result["handover_allowed"] is False
        assert [row.get("operation_id") for row in cleanup_result["unresolved_operation_refs"]] == ["next-tool"]
        assert cleanup_result["continuation_refs"] == [_CONTINUATION]
        assert restart_service.consume_restart_marker(context.paths, old_pid_alive=lambda _p: False) is None
    finally:
        turn.release.set()
        turn.thread.join(_WAIT)
    assert not turn.thread.is_alive() and turn.admitted == [True]


@pytest.mark.parametrize("veto", [None, "background_blockers", "alive_threads", "cleanup_error"])
def test_continuation_never_overrides_other_cleanup_vetoes(tmp_path, monkeypatch, veto):
    context = _context(tmp_path, monkeypatch)
    request = restart_service.submit_restart_request(context.paths, target_pid=os.getpid(), requester={"kind": "cli"},
        reason="fixture", cooldown_seconds=0)["request"]
    marker = handover._mark_drained_restart(context, request, {"ok": True, "active_turns_at_exit": 1})["restart"]
    write_json_file_atomic(context.paths.state, {"pid": os.getpid(), "status": "stopped",
        "process_identity": context.process_identity, "started_at": context.process_started_at})
    cleanup = {"drain_complete": False, "alive_threads": [], "background_blockers": [],
               "unresolved_operation_refs": [dict(_CONTINUATION)]}
    if veto == "background_blockers":
        cleanup["background_blockers"] = [{"reason": "background_future_unsettled", "ref": "original_future"}]
    elif veto == "alive_threads":
        cleanup["alive_threads"] = ["request"]
    elif veto == "cleanup_error":
        cleanup["cleanup_error"] = "OSError"
    result = handover.finalize_restart_cleanup(context, marker, cleanup)
    assert result["handover_allowed"] is (veto is None)
    assert result["status"] == ("clean_drain" if veto is None else "incomplete")
    assert result["continuation_refs"] == [_CONTINUATION]
    consumed = restart_service.consume_restart_marker(context.paths, old_pid_alive=lambda _p: False)
    assert (consumed is not None) is (veto is None)


def test_plain_stop_with_foreground_turn_in_flight_still_fails_drain(tmp_path, monkeypatch):
    context = _context(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()

    def turn():
        assert admission.try_acquire_report("user-a", 2, 4) == ""
        try:
            started.set()
            release.wait(_WAIT)
        finally:
            admission.release("user-a")

    stop = {"target_process": dict(context.process_identity), "reason": "fixture stop", "requested_at": time.time()}

    def service_loop(_context):
        assert started.wait(_WAIT)
        return {"summary": "stop requested", "stop_request": stop}

    thread = threading.Thread(target=turn)
    thread.start()
    try:
        code, spawned = _standalone(context, monkeypatch, service_loop)
        assert code == 2
        assert spawned == []
        state = read_json_file(context.paths.state)
        assert state["status"] == "failed" and state["error_code"] == "GATEWAY_DRAIN_INCOMPLETE"
        cleanup = state["cleanup_result"]
        assert cleanup["drain_complete"] is False
        assert cleanup["unresolved_operation_refs"] == [_CONTINUATION]
        assert "continuation_refs" not in cleanup
    finally:
        release.set()
        thread.join(_WAIT)
    assert not thread.is_alive()
