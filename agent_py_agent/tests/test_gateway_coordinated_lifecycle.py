"""正式协调者只消费原C/线程；使用临时真实lease，不启动W、模型或外部通道。"""
from __future__ import annotations

import os
import threading

import pytest

from agent_py_agent.agent.concurrency.restart_gate import tool_gate_closed
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import CoordinatorUnavailable
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.restart_service import (
    pending_restart_request,
    reset_restart_drain_state,
    submit_restart_request,
)
from agent_py_agent.cli import gateway_coordinated_run as coordinated
from agent_py_agent.cli import gateway_process as api
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.cli.models import (
    GatewayRunCleanupRequest,
    GatewayRunContext,
    GatewayThreadsRequest,
)
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case


# LLM: context仅运输fixture真实原C；runtime构造仍调用原control/controller门，不造ready或业务权。
# 函数用途: 为元数据与设施测试保留同代C引用。
def _context(agent, gate):
    control = gate.paths._coordinator_role_lease.control_plane
    return GatewayRunContext(None, gate.paths, agent.root / "config.yaml",
                             coordinator_worker_count=gate.count, control_plane=control,
                             drain_runtime=GatewayDrainRuntime(control))


@pytest.mark.parametrize("operation", ["restart", "stop_for_update"])
def test_internal_restart_is_cancelled_without_closing_admission_or_serving(tmp_path, monkeypatch, capsys, operation):
    reset_restart_drain_state()
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = _context(agent, gate)
        request = submit_restart_request(context.paths, target_pid=os.getpid(), requester={"kind": "cli"},
                                         reason="test", cooldown_seconds=0, operation=operation)["request"]
        assert handover.drain_for_requested_restart(context, {}) is None
        assert pending_restart_request(context.paths, pid=os.getpid()) is None
        state = read_json_file(context.paths.state)
        assert state["restart_drain"]["phase"] == "cancelled"
        assert state["restart_drain"]["request_id"] == request["request_id"]
        assert state["restart_drain"]["error_code"] == "GATEWAY_MULTIPROCESS_RESTART_UNSUPPORTED"
        assert not tool_gate_closed()
        assert context.control_plane.controller() is not None
        assert "GATEWAY_MULTIPROCESS_RESTART_UNSUPPORTED" in capsys.readouterr().err


def test_internal_handover_never_detects_manager_or_spawns(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = _context(agent, gate)
        calls = []
        monkeypatch.setattr(handover, "service_manager_restarts_gateway", lambda: calls.append("manager"))
        assert handover.hand_over_to_successor(context, pid=os.getpid(), command=["unused"],
                                               spawn=lambda *_: calls.append("spawn"),
                                               write_start_files=lambda *_: calls.append("write")) == 2
        assert calls == []


def test_revoked_context_cannot_publish_metadata_or_cancel_request(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, close):
        context = _context(agent, gate)
        submit_restart_request(context.paths, target_pid=os.getpid(), requester={}, reason="test", cooldown_seconds=0)
        before = {str(p): p.read_bytes() for p in context.paths.root.rglob("*") if p.is_file()}
        close()
        with pytest.raises(CoordinatorUnavailable):
            coordinated.publish_coordinated_state(context, {"status": "running"})
        with pytest.raises(CoordinatorUnavailable):
            handover.drain_for_requested_restart(context, {})
        assert {str(p): p.read_bytes() for p in context.paths.root.rglob("*") if p.is_file()} == before


def test_loop_start_ack_keeps_actual_threads_and_shared_stop_event(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = _context(agent, gate)
        shared_stop, entered, release = threading.Event(), threading.Event(), threading.Event()
        actual = []
        original_start = threading.Thread.start
        error = RuntimeError("test actual thread ACK")

        def target(*_):
            entered.set()
            release.wait(5)

        def ack(thread):
            actual.append(thread)
            original_start(thread)
            assert entered.wait(2)
            if len(actual) == 2:
                raise error

        for name in ("_gateway_request_loop", "_gateway_background_main_loop", "_gateway_heartbeat_loop"):
            monkeypatch.setattr(api, name, target)
        monkeypatch.setattr(threading.Thread, "start", ack)
        try:
            with pytest.raises(RuntimeError) as caught:
                coordinated.start_coordinated_loops(GatewayThreadsRequest(context, 0, 0, 0, stop_event=shared_stop))
            assert caught.value is error
            assert shared_stop.is_set()
            assert len(actual) == 2 and all(thread.is_alive() for thread in actual)
            groups = context.drain_runtime._resources
            assert groups["gateway/request-loop"].owner is actual[0]
            assert groups["gateway/background-loop"].owner is actual[1]
            assert groups["gateway/background-loop"]._fault is not None
            assert context.drain_runtime._closing is True
        finally:
            release.set()
            for thread in actual:
                thread.join(3)
            assert all(not thread.is_alive() for thread in actual)


def test_c_only_incomplete_cleanup_publishes_failure_without_fake_agent(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = _context(agent, gate)
        request = GatewayRunCleanupRequest(context, os.getpid(), threading.Event(), None, None, None, None)
        monkeypatch.setattr(api, "_install_gateway_signal_handlers", lambda *_: {})
        monkeypatch.setattr(api, "_run_gateway_service_loop", lambda *_: {"summary": "unexpected return"})
        monkeypatch.setattr(api, "_cmd_gateway_run_cleanup", lambda *_: {
            "drain_complete": False, "drain_blockers": [{"kind": "foreground", "reason": "unknown"}]})
        assert coordinated._serve_coordinated_gateway(request) == 2
        state = read_json_file(context.paths.state)
        assert state["error_code"] == "GATEWAY_DRAIN_INCOMPLETE"
        assert state["drain_blockers"] == [{"kind": "foreground", "reason": "unknown"}]
        assert state["c_run_id"] == context.control_plane.c_run_id


def test_partial_startup_uses_original_stop_and_does_not_mask_original_error(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = _context(agent, gate)
        error = RuntimeError("actual startup failure")
        cleanup = GatewayRunCleanupRequest(context, os.getpid(), threading.Event(), None, None, None, None)
        observed = []

        def incomplete(request):
            assert request.context is context and request.stop_event is cleanup.stop_event
            request.stop_event.set()
            observed.append(request)
            raise ValueError("secondary cleanup error")

        monkeypatch.setattr(api, "_cmd_gateway_run_cleanup", incomplete)
        coordinated._cleanup_starting_gateway(cleanup, error)
        assert len(observed) == 1 and cleanup.stop_event.is_set()
        state = read_json_file(context.paths.state)
        assert state["error_type"] == "RuntimeError"
        assert state["cleanup_error_type"] == "ValueError" and state["drain_complete"] is False


def test_original_paths_cannot_redirect_metadata_read_or_write(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = _context(agent, gate)
        original_path = context.paths.state
        before = original_path.read_bytes() if original_path.exists() else None
        target = tmp_path / "unexpected-state.json"
        context.paths.state = target
        with pytest.raises(CoordinatorUnavailable):
            coordinated.publish_coordinated_state(context, {"status": "running"})
        with pytest.raises(CoordinatorUnavailable):
            coordinated.coordinated_state_path(context)
        assert not target.exists()
        assert (original_path.read_bytes() if original_path.exists() else None) == before


def test_metadata_writer_uses_captured_canonical_value_after_mutation(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = _context(agent, gate)
        original_path = context.paths.state
        target = tmp_path / "unexpected-state.json"
        write = coordinated.write_json_file_atomic

        def change_after_check(path, payload):
            context.paths.state = target
            assert path == original_path
            return write(path, payload)

        monkeypatch.setattr(coordinated, "write_json_file_atomic", change_after_check)
        coordinated.publish_coordinated_state(context, {"status": "starting"})
        assert read_json_file(original_path)["c_run_id"] == context.control_plane.c_run_id
        assert not target.exists()
