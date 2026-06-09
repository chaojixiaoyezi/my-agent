from __future__ import annotations

"""Gateway status and liveness rendering helpers."""

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .daemon_control import get_running_pid, get_running_pid_report
from .io import gateway_request_counts, read_json_file_report
from .paths import GatewayPaths, gateway_chunk_path

if TYPE_CHECKING:
    from ...core import SimpleAgent


@dataclass(frozen=True)
class GatewayRunningReport:
    pid: int | None
    alive: bool
    load_error: dict | None = None


@dataclass(frozen=True)
class GatewayHeartbeatFacts:
    updated_at: float
    age_seconds: float


@dataclass(frozen=True)
class GatewayStatusRenderContext:
    paths: GatewayPaths
    running_report: GatewayRunningReport
    status: str
    heartbeat_at: float
    heartbeat_age_seconds: float


def gateway_running(paths: GatewayPaths) -> tuple[int, bool]:
    pid = get_running_pid(paths.pid)
    return pid, bool(pid)


def gateway_running_report(paths: GatewayPaths) -> GatewayRunningReport:
    pid_report = get_running_pid_report(paths.pid)
    return GatewayRunningReport(pid_report.pid, bool(pid_report.pid), pid_report.load_error)


def wait_for_gateway_running(paths: GatewayPaths, timeout: float = 10.0) -> tuple[int, bool]:
    deadline = time.time() + max(0.0, timeout)
    last_pid = 0
    while True:
        pid, alive = gateway_running(paths)
        if pid:
            last_pid = pid
        if alive:
            return pid, True
        if time.time() >= deadline:
            return pid or last_pid, False
        time.sleep(0.2)


def render_gateway_status(agent: SimpleAgent, paths: GatewayPaths) -> list[str]:
    running_report = gateway_running_report(paths)
    state_report = read_json_file_report(paths.state, context="gateway.status.state.read")
    heartbeat_report = read_json_file_report(paths.heartbeat, context="gateway.status.heartbeat.read")
    heartbeat = heartbeat_report.payload
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    heartbeat_facts = GatewayHeartbeatFacts(heartbeat_at, time.time() - heartbeat_at if heartbeat_at else 0)
    status = _gateway_status(agent, running_report, state_report.payload, heartbeat_facts)
    lines = _base_status_lines(
        GatewayStatusRenderContext(
            paths,
            running_report,
            status,
            heartbeat_facts.updated_at,
            heartbeat_facts.age_seconds,
        )
    )
    _append_status_load_errors(lines, running_report, state_report.load_error, heartbeat_report.load_error)
    return lines


def _gateway_status(
    agent: SimpleAgent,
    running_report: GatewayRunningReport,
    state: dict,
    heartbeat: GatewayHeartbeatFacts,
) -> str:
    status = "running" if running_report.alive else state.get("status", "stopped")
    if running_report.alive and heartbeat.updated_at and heartbeat.age_seconds > agent.config.gateway_stale_seconds:
        return "stale"
    return status


def _base_status_lines(context: GatewayStatusRenderContext) -> list[str]:
    now = time.time()
    lines = [
        f"gateway status={context.status} "
        f"pid={context.running_report.pid if context.running_report.pid else '-'} "
        f"alive={context.running_report.alive}",
        "gateway requests="
        + json.dumps(gateway_request_counts(context.paths), ensure_ascii=False, sort_keys=True),
    ]
    if context.heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={context.heartbeat_age_seconds:.1f}")
    _append_processing_request_lines(lines, context.paths, now)
    return lines


def _append_processing_request_lines(lines: list[str], paths: GatewayPaths, now: float) -> None:
    active_requests, load_errors, omitted_count = _processing_request_facts(paths, now)
    if active_requests:
        lines.append("gateway active_requests=" + _json_list(active_requests))
    if omitted_count:
        lines.append(f"gateway active_requests_omitted={omitted_count}")
    for load_error in load_errors:
        lines.append("gateway processing_load_error=" + _json(load_error))


def _processing_request_facts(paths: GatewayPaths, now: float) -> tuple[list[dict], list[dict], int]:
    active_requests: list[dict] = []
    load_errors: list[dict] = []
    request_paths = sorted(paths.processing.glob("*.json"))
    for request_path in request_paths[:5]:
        report = read_json_file_report(request_path, context="gateway.status.processing.read")
        if report.load_error:
            load_errors.append(report.load_error)
            continue
        active_requests.append(_processing_request_row(paths, request_path.stem, report.payload, now))
    return active_requests, load_errors, max(0, len(request_paths) - 5)


def _processing_request_row(
    paths: GatewayPaths,
    request_id: str,
    payload: dict,
    now: float,
) -> dict:
    resolved_id = str(payload.get("id") or request_id)
    row = {
        "id": resolved_id,
        "status": str(payload.get("status") or ""),
        "lease_owner": str(payload.get("lease_owner") or ""),
        "attempts": _int_value(payload.get("attempts")),
    }
    _add_age(row, "lease_age_seconds", payload.get("lease_started_at"), now)
    _add_age(row, "lease_heartbeat_age_seconds", payload.get("lease_heartbeat_at"), now)
    _add_age(row, "updated_age_seconds", payload.get("updated_at"), now)
    chunk_path = gateway_chunk_path(paths, resolved_id)
    if chunk_path.exists():
        row["chunk_stream_path"] = str(chunk_path)
    return row


def _add_age(row: dict, key: str, timestamp: object, now: float) -> None:
    timestamp_value = _float_value(timestamp)
    if timestamp_value:
        row[key] = round(max(0.0, now - timestamp_value), 1)


def _float_value(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _append_status_load_errors(
    lines: list[str],
    running_report: GatewayRunningReport,
    state_load_error: dict | None,
    heartbeat_load_error: dict | None,
) -> None:
    if state_load_error:
        lines.append("gateway state_load_error=" + _json(state_load_error))
    if heartbeat_load_error:
        lines.append("gateway heartbeat_load_error=" + _json(heartbeat_load_error))
    if running_report.load_error:
        lines.append("gateway pid_load_error=" + _json(running_report.load_error))


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_list(payload: list[dict]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
