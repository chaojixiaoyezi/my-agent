from __future__ import annotations

"""Gateway model-status tool and lifecycle diagnostic contract tests."""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.status_rendering import (
    gateway_log_diagnostics,
    gateway_runtime_snapshot,
)
from agent_py_agent.agent.tooling.gateway_status import GatewayStatusTool
from agent_py_agent.cli.gateway_process import _build_run_state
from agent_py_agent.cli.models import GatewayRunContext, GatewayThreadsRequest


def _agent(tmp_path: Path) -> SimpleNamespace:
    config = SimpleNamespace(
        gateway_workspace="gateway",
        gateway_stale_seconds=30,
        gateway_bind_host="127.0.0.1",
        gateway_port=8420,
        model_name="MiniMax-M2.7",
    )
    return SimpleNamespace(
        root=tmp_path,
        config=config,
        subagents=SimpleNamespace(workspace=tmp_path / "subagents"),
    )


def _running_gateway(tmp_path: Path) -> tuple[SimpleNamespace, object]:
    agent = _agent(tmp_path)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for path in (
        paths.root,
        paths.inbox,
        paths.processing,
        paths.done,
        paths.failed,
        paths.responses,
    ):
        path.mkdir(parents=True, exist_ok=True)
    write_pid_record(paths.pid)
    paths.state.write_text(
        json.dumps(
            {
                "status": "running",
                "pid": os.getpid(),
                "started_at": time.time() - 5,
                "config_path": "/tmp/testbox-single-gateway.yaml",
                "model_name": "MiniMax-M2.7",
                "http_bind_host": "127.0.0.1",
                "http_port": 8420,
                "log_start_offset_bytes": 0,
            }
        ),
        encoding="utf-8",
    )
    paths.heartbeat.write_text(
        json.dumps(
            {
                "updated_at": time.time(),
                "queue_ages": {
                    "oldest_pending_age_seconds": 1.25,
                    "oldest_processing_age_seconds": 2.5,
                },
                "inflight": {"active": 1},
            }
        ),
        encoding="utf-8",
    )
    paths.log.write_text("gateway ready\n", encoding="utf-8")
    return agent, paths


def test_gateway_log_diagnostics_excludes_previous_lifecycle(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.log"
    previous = "Traceback (most recent call last):\nBrokenPipeError: old client\n"
    current = "gateway ready\nRuntimeError: new lifecycle fault\n"
    log_path.write_text(previous + current, encoding="utf-8")

    result = gateway_log_diagnostics(
        log_path,
        start_offset_bytes=len(previous.encode("utf-8")),
    )

    assert result["status"] == "noise_observed"
    assert result["exception_counts"] == {"RuntimeError": 1}
    assert result["traceback_count"] == 0
    assert result["raw_log_included"] is False


def test_gateway_log_diagnostics_does_not_call_zero_observation_quiet(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "gateway.log"
    existing = "old lifecycle line\n"
    log_path.write_text(existing, encoding="utf-8")

    result = gateway_log_diagnostics(
        log_path,
        start_offset_bytes=len(existing.encode("utf-8")),
    )

    assert result["status"] == "no_new_log_bytes"
    assert result["current_lifecycle_bytes"] == 0
    assert result["evidence_scope"] == "current_lifecycle_bounded_file_tail"


def test_gateway_runtime_snapshot_reports_canonical_endpoint_and_identity(
    tmp_path: Path,
) -> None:
    agent, paths = _running_gateway(tmp_path)

    snapshot = gateway_runtime_snapshot(agent, paths, include_log_diagnostics=True)

    assert snapshot["alive"] is True
    assert snapshot["identity"]["pid"] == os.getpid()
    assert snapshot["http"] == {
        "bind_host": "127.0.0.1",
        "port": 8420,
        "base_url": "http://127.0.0.1:8420",
        "status_path": "/status",
        "metrics_path": "/metrics",
    }
    assert snapshot["identity"]["model_name"] == "MiniMax-M2.7"
    assert snapshot["identity"]["config_path"] == "/tmp/testbox-single-gateway.yaml"
    assert snapshot["log_diagnostics"]["status"] == "quiet"
    assert "api_key" not in json.dumps(snapshot).lower()


def test_gateway_status_tool_returns_snapshot_without_raw_log(tmp_path: Path) -> None:
    agent, _ = _running_gateway(tmp_path)

    result = GatewayStatusTool(agent).execute({})
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["schema"] == "gateway_runtime_snapshot.v1"
    assert result.output.index('"identity"') < result.output.index('"heartbeat"')
    assert result.output.count("MiniMax-M2.7") == 1
    assert payload["http"]["port"] == 8420
    assert payload["identity"]["model_name"] == "MiniMax-M2.7"
    assert payload["log_diagnostics"]["raw_log_included"] is False
    assert "gateway ready" not in result.output


def test_gateway_run_state_persists_model_endpoint_and_log_boundary(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    context = GatewayRunContext(
        agent=agent,
        paths=gateway_paths_from_root(tmp_path / "gateway"),
        config_path=tmp_path / "config.yaml",
        log_start_offset_bytes=4321,
    )
    request = GatewayThreadsRequest(
        context=context,
        requeued=2,
        failed=1,
        http_port=8420,
    )

    state = _build_run_state(request, os.getpid())

    assert state["config_path"] == str(tmp_path / "config.yaml")
    assert state["model_name"] == "MiniMax-M2.7"
    assert state["http_bind_host"] == "127.0.0.1"
    assert state["http_port"] == 8420
    assert state["log_start_offset_bytes"] == 4321
