"""B1a：就绪前端口读回、投影陈旧和内部单例默认关闭。"""
from __future__ import annotations

import json
import os
import time

import pytest

from agent_py_agent.agent.attempt import sandbox
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts import multiprocess_state as state
from agent_py_agent.agent.gateway_parts.daemon_metadata import build_process_identity
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition


def _launch(tmp_path):
    return mode.WorkerLaunch(WorkerPartition(0, 2, "generation-a"), tmp_path / "gateway", tmp_path / "home", os.getppid())


def _state(launch):
    return {"c_run_id": launch.partition.c_run_id, "pid": launch.parent_pid,
            "process_identity": build_process_identity(launch.parent_pid), "http_port": 43127}


def _write_state(launch, payload):
    launch.gateway_root.mkdir(parents=True, exist_ok=True)
    (launch.gateway_root / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")


def test_ready_requires_actual_port_registered_and_read_back(tmp_path, monkeypatch):
    launch = _launch(tmp_path)
    _write_state(launch, _state(launch))
    monkeypatch.setattr(sandbox, "_LOCAL_GATEWAY_PORTS", set())
    result = state.prepare_worker_readiness(launch)
    assert result["ready"] is True
    assert sandbox.gateway_bound_ports() == (43127,)
    assert result["gateway_ports"] == [43127]


@pytest.mark.parametrize("fault", ["missing", "corrupt", "old", "no-port", "bad-port", "bad-pid"])
def test_missing_corrupt_old_and_invalid_coordinator_state_never_ready(tmp_path, monkeypatch, fault):
    launch = _launch(tmp_path)
    payload = _state(launch)
    mutations = {"old": lambda: payload.update(c_run_id="generation-old"),
                 "no-port": lambda: payload.pop("http_port"), "bad-port": lambda: payload.update(http_port=True),
                 "bad-pid": lambda: payload["process_identity"].update(pid=launch.parent_pid + 1)}
    if fault in mutations:
        mutations[fault]()
    if fault != "missing":
        _write_state(launch, payload)
    if fault == "corrupt":
        (launch.gateway_root / "gateway_state.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(sandbox, "_LOCAL_GATEWAY_PORTS", set())
    result = state.prepare_worker_readiness(launch)
    assert result["ready"] is False
    assert result["error_code"] == "WORKER_NOT_READY"
    assert result["reason"]
    assert sandbox.gateway_bound_ports() == ()


def test_port_registration_without_matching_readback_is_not_ready(tmp_path, monkeypatch):
    launch = _launch(tmp_path)
    _write_state(launch, _state(launch))
    monkeypatch.setattr(sandbox, "register_gateway_bound_port", lambda port: None)
    monkeypatch.setattr(sandbox, "gateway_bound_ports", lambda: ())
    assert state.prepare_worker_readiness(launch)["ready"] is False


def test_heartbeat_and_projection_never_grant_execution_or_death_proof(tmp_path):
    launch = _launch(tmp_path)
    heartbeat = state.worker_heartbeat(launch, {"ready": True, "gateway_ports": [43127]}, 100.0)
    assert heartbeat is not None
    assert heartbeat["pid"] == os.getpid()
    assert heartbeat["process_identity"] == build_process_identity()
    assert heartbeat["inflight"] == 0
    assert heartbeat["loop_health"] == "healthy"
    assert state.stampable_workers([heartbeat], launch.partition, 115.0) == [0]
    assert state.stampable_workers([heartbeat], launch.partition, 115.01) == []
    assert state.stampable_workers([{**heartbeat, "c_run_id": "old"}], launch.partition, 101.0) == []
    assert state.stampable_workers([{**heartbeat, "ready": False}], launch.partition, 101.0) == []
    assert "dead" not in heartbeat and "claim" not in heartbeat


def test_control_commands_are_filtered_by_generation():
    assert state.worker_control_action({"c_run_id": "old", "exit": True}, "now") == ""
    assert state.worker_control_action({"c_run_id": "now", "drain": True}, "now") == "drain"
    assert state.worker_control_action({"c_run_id": "now", "exit": True}, "now") == "exit"


def test_default_single_process_guard_does_not_touch_lock_file(tmp_path):
    with coordinator_guard(tmp_path, 0):
        assert not (tmp_path / "coordinator.lock").exists()
    assert list(tmp_path.iterdir()) == []




def test_worker_mode_cannot_be_enabled_by_cli_or_request_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_AGENT_GATEWAY_WORKER", raising=False)
    assert mode.establish_worker_mode(_launch(tmp_path))["error_code"] == "WORKER_MODE_REQUIRED"
    assert mode.current_worker_partition() is None


def test_windows_worker_is_structurally_rejected_before_bootstrap(monkeypatch, capsys):
    from agent_py_agent.cli import gateway_worker

    monkeypatch.setattr(gateway_worker.sys, "platform", "win32")
    assert gateway_worker.dispatch_worker_entry(["gateway", "worker", "--index", "0", "--of", "2", "--run", "test"]) == 2
    assert json.loads(capsys.readouterr().err)["error_code"] == "WORKER_PLATFORM_UNSUPPORTED"


@pytest.mark.parametrize("code", [
    "COORDINATOR_ALREADY_RUNNING", "OWNER_ROUTE_MISMATCH", "WORKER_NOT_READY",
    "WORKER_MODE_REQUIRED", "WORKER_MODE_INVALID", "WORKER_PLATFORM_UNSUPPORTED",
])
def test_worker_rejections_are_registered_without_automatic_replay(code):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.contracts.recovery import RecoveryAction

    contract = error_contract(code)
    assert contract.code == code
    assert contract.retryable is False
    assert contract.recommended_action == RecoveryAction.REPORT_BLOCKER.value
