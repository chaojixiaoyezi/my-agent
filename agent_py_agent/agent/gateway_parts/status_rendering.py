from __future__ import annotations

"""Gateway status and liveness rendering helpers."""

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .daemon_control import get_running_pid, get_running_pid_report
from .io import gateway_request_counts, read_json_file_report
from .paths import GatewayPaths

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
    lines = [
        f"gateway status={context.status} "
        f"pid={context.running_report.pid if context.running_report.pid else '-'} "
        f"alive={context.running_report.alive}",
        "gateway requests="
        + json.dumps(gateway_request_counts(context.paths), ensure_ascii=False, sort_keys=True),
    ]
    if context.heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={context.heartbeat_age_seconds:.1f}")
    return lines


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
