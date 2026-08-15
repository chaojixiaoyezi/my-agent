from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts.channel_health import adapter_runtime_health


def _agent(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
        config=SimpleNamespace(gateway_workspace="gateway"),
    )


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid}), encoding="utf-8")


def test_adapter_runtime_health_reads_fresh_per_channel_snapshot(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway"
    _write_pid(gateway / "adapter.pid", os.getpid())
    (gateway / "adapter_state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "channels": [
                    {
                        "name": "feishu",
                        "health": {"state": "healthy", "error_code": ""},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = adapter_runtime_health(_agent(tmp_path))

    assert result["feishu"]["state"] == "healthy"
    assert result["feishu"]["error_code"] == ""


def test_adapter_runtime_health_rejects_stale_heartbeat(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway"
    _write_pid(gateway / "adapter.pid", os.getpid())
    (gateway / "adapter_state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = adapter_runtime_health(_agent(tmp_path))

    assert result["*"]["state"] == "unhealthy"
    assert result["*"]["error_code"] == "CHANNEL_ADAPTER_HEARTBEAT_STALE"


def test_adapter_runtime_health_fails_closed_when_process_is_dead(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway"
    _write_pid(gateway / "adapter.pid", 999_999_999)
    (gateway / "adapter_state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = adapter_runtime_health(_agent(tmp_path))

    assert result["*"]["state"] == "unhealthy"
    assert result["*"]["error_code"] == "CHANNEL_ADAPTER_NOT_RUNNING"
