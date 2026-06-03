
from __future__ import annotations

"""JSON payload helpers for the local status command."""

from dataclasses import dataclass
from typing import Any

from .shared_progress import shared_progress_for_board


@dataclass(frozen=True)
class GatewayStatusRequest:
    alive: bool
    gateway_state: dict
    heartbeat: dict
    stale_seconds: float
    now: float


def resolve_gateway_status(request: GatewayStatusRequest) -> tuple[str, float]:
    heartbeat_at = float(request.heartbeat.get("updated_at", 0) or 0)
    heartbeat_age = request.now - heartbeat_at if heartbeat_at else 0
    state_status = request.gateway_state.get("status", "stopped")
    gateway_status = "running" if request.alive else ("stopped" if state_status == "running" else state_status)
    if request.alive and heartbeat_at and heartbeat_age > request.stale_seconds:
        gateway_status = "stale"
    return gateway_status, heartbeat_age


@dataclass
class StatusPayloadContext:
    agent: Any
    paths: Any
    local_stats: dict
    board: Any
    timeline: list
    pid: int | None
    alive: bool
    gateway_status: str
    heartbeat_age: float
    active_work_summary: Any
    request_counts: dict
    gateway_state_load_error: dict | None = None
    gateway_heartbeat_load_error: dict | None = None


def build_status_payload(ctx: StatusPayloadContext) -> dict:
    return {
        "agent_name": ctx.agent.config.agent_name,
        "workspace_root": str(ctx.agent.root),
        "gateway": _gateway_payload(ctx),
        "local_store": ctx.local_stats,
        "subagents": _subagents_payload(ctx),
        "active_work": _active_work_payload(ctx.active_work_summary),
        "timeline": [item.__dict__ for item in ctx.timeline],
    }


def _gateway_payload(ctx: StatusPayloadContext) -> dict:
    payload = {
        "status": ctx.gateway_status,
        "pid": ctx.pid,
        "alive": ctx.alive,
        "heartbeat_age_seconds": round(ctx.heartbeat_age, 1) if ctx.heartbeat_age else 0,
        "request_counts": ctx.request_counts,
        "workspace": str(ctx.paths.root),
    }
    if ctx.gateway_state_load_error:
        payload["state_load_error"] = ctx.gateway_state_load_error
    if ctx.gateway_heartbeat_load_error:
        payload["heartbeat_load_error"] = ctx.gateway_heartbeat_load_error
    return payload


def _active_work_payload(active_work_summary: Any) -> dict | None:
    if not active_work_summary:
        return None
    return {
        "gateway_alive": active_work_summary.gateway_alive,
        "active_task_count": active_work_summary.active_task_count,
        "stale_request_count": active_work_summary.stale_request_count,
        "recent_tasks": active_work_summary.recent_tasks,
    }


def _subagents_payload(ctx: StatusPayloadContext) -> dict:
    return {
        "summary": ctx.board.summary,
        "hot_count": len(ctx.board.hot_list),
        "recent_count": len(ctx.board.recent),
        "hot": [item.__dict__ for item in ctx.board.hot_list[: ctx.agent.config.subagent_board_limit]],
        "recent": [item.__dict__ for item in ctx.board.recent[: ctx.agent.config.subagent_board_limit]],
        "shared_progress": shared_progress_for_board(ctx.agent, ctx.board, purpose="status"),
    }
