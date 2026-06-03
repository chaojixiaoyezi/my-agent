from __future__ import annotations

"""Runtime error visibility tests for gateway CLI status rendering."""

from pathlib import Path
from unittest.mock import MagicMock


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
