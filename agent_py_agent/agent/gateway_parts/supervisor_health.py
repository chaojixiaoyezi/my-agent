from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .io import read_json_file_report


@dataclass(frozen=True)
class SupervisorPayloadReport:
    payload: dict | None
    load_error: dict | None = None


def read_adapter_state(state_path: Path) -> dict:
    return read_adapter_state_report(state_path).payload or {}


def read_adapter_state_report(state_path: Path) -> SupervisorPayloadReport:
    if not state_path.exists():
        return SupervisorPayloadReport(None)
    report = read_json_file_report(state_path, context="gateway.supervisor.adapter_state.read")
    return SupervisorPayloadReport(report.payload if report.payload else None, report.load_error)


def read_gateway_heartbeat_report(heartbeat_path: Path) -> SupervisorPayloadReport:
    if not heartbeat_path.exists():
        return SupervisorPayloadReport(None)
    report = read_json_file_report(heartbeat_path, context="gateway.supervisor.heartbeat.read")
    return SupervisorPayloadReport(report.payload if report.payload else None, report.load_error)


def record_health_load_error(
    load_errors: list[dict],
    load_error: dict,
    *,
    log_warn: Callable[[str], None],
) -> None:
    load_errors.append(load_error)
    log_warn(
        "Gateway health load error: "
        f"context={load_error.get('context', '')} "
        f"path={load_error.get('path', '')} "
        f"error={load_error.get('message', '')}"
    )
