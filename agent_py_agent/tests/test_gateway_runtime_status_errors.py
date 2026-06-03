from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.gateway_parts.runtime_status import (
    WriteRuntimeStatusParams,
    read_runtime_status,
    read_runtime_status_report,
    write_runtime_status,
)


def test_read_runtime_status_report_exposes_bad_json(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text("{bad json", encoding="utf-8")

    report = read_runtime_status_report(status_path)

    assert report.payload is None
    assert report.load_error is not None
    assert report.load_error["context"] == "gateway.runtime_status.read"
    assert read_runtime_status(status_path) is None


def test_write_runtime_status_preserves_previous_load_error(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text("{bad json", encoding="utf-8")

    write_runtime_status(WriteRuntimeStatusParams(status_path=status_path, gateway_state="running"))

    status = read_runtime_status(status_path)
    assert status is not None
    assert status["gateway_state"] == "running"
    assert status["previous_status_load_error"]["context"] == "gateway.runtime_status.read"
