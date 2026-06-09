
from __future__ import annotations

"""JSON payload helpers for the local status command."""

import time
from dataclasses import dataclass
from typing import Any

from ..agent.startup_recovery import is_recent_board_item
from .shared_progress import shared_progress_for_board

_SUBAGENT_GOAL_PREVIEW_CHARS = 180


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
    archive_request_counts: dict | None = None
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
        "archive_request_counts": ctx.archive_request_counts or {},
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
    payload = {
        "gateway_alive": active_work_summary.gateway_alive,
        "active_task_count": active_work_summary.active_task_count,
        "stale_request_count": active_work_summary.stale_request_count,
        "recent_tasks": active_work_summary.recent_tasks,
    }
    errors = list(getattr(active_work_summary, "detection_errors", []) or [])
    if errors:
        payload["detection_errors"] = errors
    return payload


def _subagents_payload(ctx: StatusPayloadContext) -> dict:
    hot = _current_hot_items(ctx.board.hot_list, now=time.time())
    recent = list(ctx.board.recent[: ctx.agent.config.subagent_board_limit])
    return {
        "summary": ctx.board.summary,
        "current_summary": _status_items_summary([*hot, *recent]),
        "hot_count": len(hot),
        "historical_hot_count": max(0, len(ctx.board.hot_list) - len(hot)),
        "recent_count": len(ctx.board.recent),
        "hot": [_board_item_payload(item) for item in hot[: ctx.agent.config.subagent_board_limit]],
        "recent": [_board_item_payload(item) for item in recent],
        "shared_progress": shared_progress_for_board(ctx.agent, ctx.board, purpose="status"),
    }


def _current_hot_items(items: list[Any], *, now: float) -> list[Any]:
    return [item for item in items if is_recent_board_item(item, now=now)]


def _status_items_summary(items: list[Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"visible_total": len(items), "by_status": {}}
    by_status: dict[str, int] = summary["by_status"]
    for item in items:
        status = str(getattr(item, "status", "") or "UNKNOWN")
        by_status[status] = by_status.get(status, 0) + 1
    return summary


def _board_item_payload(item: Any) -> dict[str, Any]:
    goal = str(getattr(item, "goal", "") or "")
    payload: dict[str, Any] = {
        "id": str(getattr(item, "id", "") or ""),
        "agent_name": str(getattr(item, "agent_name", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "verification_status": str(getattr(item, "verification_status", "") or ""),
        "channel_status": str(getattr(item, "channel_status", "") or ""),
        "role": str(getattr(item, "role", "") or ""),
        "depth": _int_attr(item, "depth"),
        "owner": str(getattr(item, "owner", "") or ""),
        "final_owner": str(getattr(item, "final_owner", "") or ""),
        "parent_id": str(getattr(item, "parent_id", "") or ""),
        "root_id": str(getattr(item, "root_id", "") or ""),
        "goal": _goal_preview(goal),
        "goal_truncated": len(goal.replace("\n", " ").strip()) > _SUBAGENT_GOAL_PREVIEW_CHARS,
        "progress": _float_attr(item, "progress"),
        "created_at": _float_attr(item, "created_at"),
        "updated_at": _float_attr(item, "updated_at"),
        "heartbeat_at": _float_attr(item, "heartbeat_at"),
        "running_seconds": _float_attr(item, "running_seconds"),
        "seconds_since_progress": _float_attr(item, "seconds_since_progress"),
        "evidence_count": _int_attr(item, "evidence_count"),
        "open_request_count": _int_attr(item, "open_request_count"),
        "open_gap_count": _int_attr(item, "open_gap_count"),
        "blocker_count": _int_attr(item, "blocker_count"),
        "finding_count": _int_attr(item, "finding_count"),
        "child_count": _int_attr(item, "child_count"),
        "risk_flags": _string_list(getattr(item, "risk_flags", []), limit=8),
        "artifact_refs": _string_list(getattr(item, "artifact_refs", []), limit=3),
        "evidence_refs": _string_list(getattr(item, "evidence_refs", []), limit=3),
    }
    return payload


def _goal_preview(value: object) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= _SUBAGENT_GOAL_PREVIEW_CHARS:
        return text
    return text[:_SUBAGENT_GOAL_PREVIEW_CHARS].rstrip() + "..."


def _int_attr(item: Any, name: str) -> int:
    try:
        return int(getattr(item, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _float_attr(item: Any, name: str) -> float:
    try:
        return float(getattr(item, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _string_list(values: object, *, limit: int) -> list[str]:
    if not isinstance(values, list | tuple | set):
        return []
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result
