from __future__ import annotations

"""Runtime error visibility tests for gateway CLI status rendering."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _gateway_paths(tmp_path: Path):
    from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

    return GatewayPaths(
        root=tmp_path / "gateway",
        pid=tmp_path / "gateway/gateway.pid",
        adapter_pid=tmp_path / "gateway/adapter.pid",
        state=tmp_path / "gateway/state.json",
        heartbeat=tmp_path / "gateway/heartbeat.json",
        stop_request=tmp_path / "gateway/stop.request",
        log=tmp_path / "gateway/gateway.log",
        inbox=tmp_path / "gateway/requests/pending",
        processing=tmp_path / "gateway/requests/processing",
        done=tmp_path / "gateway/requests/done",
        failed=tmp_path / "gateway/requests/failed",
        responses=tmp_path / "gateway/responses",
        history=tmp_path / "gateway/gateway_requests.jsonl",
    )


def test_render_gateway_status_reports_bad_state_and_heartbeat(tmp_path: Path):
    """CLI status should not hide corrupted gateway state files."""
    from agent_py_agent.agent.gateway_parts.queue_service import render_gateway_status

    paths = _gateway_paths(tmp_path)
    paths.root.mkdir(parents=True)
    paths.state.write_text("{bad state", encoding="utf-8")
    paths.heartbeat.write_text("[bad heartbeat]", encoding="utf-8")
    agent = MagicMock()
    agent.config.gateway_stale_seconds = 60

    lines = render_gateway_status(agent, paths)

    assert any("gateway state_load_error=" in line for line in lines)
    assert any("gateway.status.state.read" in line for line in lines)
    assert any("gateway heartbeat_load_error=" in line for line in lines)
    assert any("gateway.status.heartbeat.read" in line for line in lines)


def test_render_gateway_status_reports_bad_pid_record(tmp_path: Path):
    """CLI status should show corrupted PID records instead of only stopped."""
    from agent_py_agent.agent.gateway_parts.queue_service import render_gateway_status

    paths = _gateway_paths(tmp_path)
    paths.root.mkdir(parents=True)
    paths.pid.write_text("{bad pid", encoding="utf-8")
    agent = MagicMock()
    agent.config.gateway_stale_seconds = 60

    lines = render_gateway_status(agent, paths)

    assert any("gateway pid_load_error=" in line for line in lines)
    assert any("gateway.pid_record.read" in line for line in lines)


def test_render_gateway_status_reports_processing_request_facts(tmp_path: Path, monkeypatch):
    """CLI status should show who owns an active gateway request."""
    from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path
    from agent_py_agent.agent.gateway_parts.queue_service import render_gateway_status

    paths = _gateway_paths(tmp_path)
    paths.processing.mkdir(parents=True)
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.status_rendering.time.time", lambda: 1000.0)
    request_id = "req-active"
    (paths.processing / f"{request_id}.json").write_text(
        json.dumps(
            {
                "id": request_id,
                "status": "processing",
                "attempts": 3,
                "lease_owner": "gw-worker-2",
                "lease_started_at": 900.0,
                "lease_heartbeat_at": 970.0,
                "updated_at": 980.0,
            }
        ),
        encoding="utf-8",
    )
    chunk_path = gateway_chunk_path(paths, request_id)
    chunk_path.write_text('{"delta":"hello"}\n', encoding="utf-8")
    agent = MagicMock()
    agent.config.gateway_stale_seconds = 60

    lines = render_gateway_status(agent, paths)

    active_line = next(line for line in lines if line.startswith("gateway active_requests="))
    assert '"id": "req-active"' in active_line
    assert '"lease_owner": "gw-worker-2"' in active_line
    assert '"attempts": 3' in active_line
    assert '"lease_age_seconds": 100.0' in active_line
    assert '"lease_heartbeat_age_seconds": 30.0' in active_line
    assert '"updated_age_seconds": 20.0' in active_line
    assert str(chunk_path) in active_line


def test_render_gateway_status_reports_bad_processing_record(tmp_path: Path):
    """CLI status should expose corrupted processing request records."""
    from agent_py_agent.agent.gateway_parts.queue_service import render_gateway_status

    paths = _gateway_paths(tmp_path)
    paths.processing.mkdir(parents=True)
    (paths.processing / "bad.json").write_text("{bad processing", encoding="utf-8")
    agent = MagicMock()
    agent.config.gateway_stale_seconds = 60

    lines = render_gateway_status(agent, paths)

    assert any("gateway processing_load_error=" in line for line in lines)
    assert any("gateway.status.processing.read" in line for line in lines)


def test_wait_for_gateway_running_honors_monotonic_deadline(monkeypatch, tmp_path: Path):
    """Gateway readiness timeout should not oversleep its configured budget."""
    from agent_py_agent.agent.gateway_parts import status_rendering

    clock = {"now": 10.0}
    sleeps: list[float] = []

    monkeypatch.setattr(status_rendering, "gateway_running", lambda _paths: (0, False))
    monkeypatch.setattr(status_rendering.time, "monotonic", lambda: clock["now"])

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(status_rendering.time, "sleep", advance)

    assert status_rendering.wait_for_gateway_running(_gateway_paths(tmp_path), 0.45) == (0, False)
    assert sum(sleeps) == pytest.approx(0.45)
    assert max(sleeps) <= 0.2


def test_render_gateway_status_reports_unidentified_stale_attempts_only_when_present(tmp_path: Path):
    """gateway status 只在 state.json 计数大于 0 时提醒无身份悬挂运行轮，不查库、不结清。"""
    from agent_py_agent.agent.gateway_parts.queue_service import render_gateway_status

    paths = _gateway_paths(tmp_path)
    paths.root.mkdir(parents=True)
    agent = MagicMock()
    agent.config.gateway_stale_seconds = 60

    paths.state.write_text(json.dumps({"status": "stopped", "unidentified_stale_attempts": 9}), encoding="utf-8")
    assert "gateway unidentified_stale_attempts=9" in render_gateway_status(agent, paths)

    paths.state.write_text(json.dumps({"status": "stopped", "unidentified_stale_attempts": 0}), encoding="utf-8")
    assert not any("unidentified_stale_attempts" in line for line in render_gateway_status(agent, paths))
