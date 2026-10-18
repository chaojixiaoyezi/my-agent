"""C循环仅消费原control/Root/lease；临时实际登记与fake tick，不启动物理W或模型。"""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
    CoordinatorBackgroundRequests,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case


# LLM: 原fixture确有flock/control/controller，runtime仅保存其同源引用；不通过fake role或fake ready构造正来源。
# 函数用途: 为纯循环测试投影同一真实C，保留agent None的冷组合边界。
def cold_context(agent, gate):
    control = gate.paths._coordinator_role_lease.control_plane
    runtime = GatewayDrainRuntime(control)
    return GatewayRunContext(
        None,
        gate.paths,
        agent.root / "config.yaml",
        coordinator_worker_count=gate.count,
        control_plane=control,
        drain_runtime=runtime,
    )


def test_actual_c_request_loop_waits_without_constructing_inline_dispatcher(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        context = cold_context(agent, gate)
        calls = []

        def forbidden(*args):
            calls.append(args)
            raise AssertionError("cold C must not build business dispatcher")

        monkeypatch.setattr(loops, "_RequestDispatcher", forbidden)
        stop = threading.Event()
        stop.set()
        loops._gateway_request_loop(context, gate.paths, stop)
        assert calls == []
        assert loops.loop_health.snapshot()["dispatcher_state"] == "exited"
        assert loops.loop_health.snapshot()["dispatcher_exit_error"] == {}


def test_actual_c_background_loop_uses_original_sources_and_no_legacy_agents(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        agent.config.owner_maintenance_scan_interval_seconds = 0
        context = cold_context(agent, gate)
        stop = threading.Event()
        observed = []
        original = CoordinatorBackgroundRequests.tick

        def real_tick(source):
            try:
                observed.append((source.control, source.runtime))
                return original(source)
            finally:
                stop.set()

        def forbidden(*args):
            raise AssertionError("C must not create original business/owner control lane")

        monkeypatch.setattr(CoordinatorBackgroundRequests, "tick", real_tick)
        for name in (
            "_BackgroundMainSupervisor",
            "_start_orphan_reconcile_loop",
            "_start_owner_maintenance_loop",
            "_start_scheduler_due_loop",
        ):
            monkeypatch.setattr(loops, name, forbidden)
        loops._gateway_background_main_loop(context, stop)
        assert observed == [(context.control_plane, context.drain_runtime)]


@pytest.mark.parametrize(
    "fault", ["missing_control", "missing_runtime", "copied_runtime", "wrong_count", "closed"]
)
def test_c_source_fault_never_falls_back_to_business_lanes(tmp_path, monkeypatch, fault):
    import copy

    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, close):
        context = cold_context(agent, gate)
        if fault == "missing_control":
            context = replace(context, control_plane=None)
        elif fault == "missing_runtime":
            context = replace(context, drain_runtime=None)
        elif fault == "copied_runtime":
            context = replace(context, drain_runtime=copy.copy(context.drain_runtime))
        elif fault == "wrong_count":
            context = replace(context, coordinator_worker_count=3)
        else:
            close()
        forbidden = []
        monkeypatch.setattr(
            loops, "_BackgroundMainSupervisor", lambda *args: forbidden.append(True)
        )
        stop = threading.Event()
        stop.set()
        with pytest.raises(ValueError):
            loops._gateway_background_main_loop(context, stop)
        assert forbidden == []


def test_c_heartbeat_uses_original_lease_not_business_agent_and_refuses_replacement(
    tmp_path, monkeypatch
):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, close):
        context = cold_context(agent, gate)
        loops._write_gateway_heartbeat(gate.paths, None, status="running", pid=os.getpid())
        payload = read_json_file(gate.paths.heartbeat)
        assert (
            payload["status"] == "running"
            and payload["pid"] == os.getpid()
            and payload["subagent_workspace"] == ""
        )
        assert payload["gateway_workspace"] == str(gate.paths.root)
        before = gate.paths.heartbeat.read_bytes()
        close()
        with pytest.raises(RuntimeError):
            loops._write_gateway_heartbeat(gate.paths, None, status="running", pid=os.getpid())
        assert gate.paths.heartbeat.read_bytes() == before


@pytest.mark.parametrize("fault", ["missing_marker", "copied_paths", "replaced_marker"])
def test_heartbeat_last_writer_requires_original_paths_lease_marker(tmp_path, monkeypatch, fault):
    import copy

    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        original = gate.paths
        loops._write_gateway_heartbeat(original, None, status="running", pid=os.getpid())
        before = original.heartbeat.read_bytes()
        paths = copy.copy(original) if fault == "copied_paths" else original
        if fault == "missing_marker":
            monkeypatch.delattr(paths, "_coordinator_role_lease")
        elif fault == "replaced_marker":
            monkeypatch.setattr(paths, "_coordinator_role_lease", object())
        with pytest.raises(RuntimeError):
            loops._write_gateway_heartbeat(paths, None, status="running", pid=os.getpid())
        assert original.heartbeat.read_bytes() == before
