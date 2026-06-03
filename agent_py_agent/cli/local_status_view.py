
from __future__ import annotations

"""human-readable status rendering helpers for local CLI commands."""

import json
from dataclasses import dataclass
from typing import Any

from .common import format_local_time
from .shared_progress import (
    format_shared_progress_lines,
    format_takeover_view_lines,
)


def _format_takeover_view_section(panels: list[dict]) -> None:
    print("Takeover View")
    for line in format_takeover_view_lines(panels):
        print(line)


@dataclass
class StatusPrintContext:
    agent: Any
    paths: Any
    local_stats: dict
    board: Any
    timeline: list
    gateway_status: str
    pid: int | None
    alive: bool
    heartbeat_age: float
    active_work_summary: Any
    request_counts: dict
    suggested_actions: list
    gateway_state_load_error: dict | None = None
    gateway_heartbeat_load_error: dict | None = None


@dataclass
class _GatewaySectionRequest:
    status: str
    pid: int | None
    alive: bool
    heartbeat_age: float
    paths: Any
    state_load_error: dict | None = None
    heartbeat_load_error: dict | None = None


def print_status_human(ctx: StatusPrintContext):
    agent = ctx.agent
    print("MY-AGENT STATUS")
    print(f"agent={agent.config.agent_name}")
    print(f"workspace={agent.root}")
    print("")
    _format_gateway_section(
        _GatewaySectionRequest(
            ctx.gateway_status,
            ctx.pid,
            ctx.alive,
            ctx.heartbeat_age,
            ctx.paths,
            ctx.gateway_state_load_error,
            ctx.gateway_heartbeat_load_error,
        ),
        ctx.request_counts,
    )
    print("")
    print("Local Store")
    print(f"- records={ctx.local_stats['record_count']} events={ctx.local_stats['event_count']} fts5={ctx.local_stats['fts5_enabled']}")
    print(f"- db={ctx.local_stats['db_path']}")
    print("")
    _format_active_work_block(ctx.active_work_summary)
    print("")
    _format_subagents_section(ctx.board, agent.config.subagent_board_limit)
    print("")
    _format_shared_progress_section(getattr(ctx.board, "shared_progress", []))
    print("")
    _format_takeover_view_section(getattr(ctx.board, "shared_progress", []))
    print("")
    _format_timeline(ctx.timeline)
    print("")
    _format_suggested_actions(ctx.suggested_actions)


def _format_gateway_section(request: _GatewaySectionRequest, request_counts: dict) -> None:
    print("Gateway")
    print(f"- status={request.status} pid={request.pid if request.pid else '-'} alive={request.alive}")
    if request.heartbeat_age:
        print(f"- heartbeat_age_seconds={request.heartbeat_age:.1f}")
    if request.state_load_error:
        print("- state_load_error=" + json.dumps(request.state_load_error, ensure_ascii=False, sort_keys=True))
    if request.heartbeat_load_error:
        print("- heartbeat_load_error=" + json.dumps(request.heartbeat_load_error, ensure_ascii=False, sort_keys=True))
    print("- requests=" + json.dumps(request_counts, ensure_ascii=False, sort_keys=True))
    print(f"- workspace={request.paths.root}")


def _format_active_work_block(active_work_summary) -> None:
    if active_work_summary:
        _format_active_work(active_work_summary)
        return
    print("进行中任务")
    print("- 暂无")


def _format_active_work(active_work_summary) -> None:
    from ..agent.startup_recovery import format_active_work_summary

    print("进行中任务")
    print("-" + format_active_work_summary(active_work_summary).replace("\n", "\n  - "))
    if active_work_summary.active_task_count > 0:
        print("  运行 my-agent subagents-dispatch 可继续调度")


def _format_subagents_section(board, limit: int) -> None:
    print("Subagents")
    print("- summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    if board.hot_list:
        print(f"- hot={len(board.hot_list)}")
        for item in board.hot_list[: limit]:
            flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
            print(f"  - {item.id} {item.status}/{item.verification_status} flags={flags} :: {item.goal}")
    else:
        print("- hot=0")
    if board.recent:
        print("- recent:")
        for item in board.recent[: limit]:
            print(f"  - {item.id} {item.status}/{item.verification_status} :: {item.goal}")


def _format_shared_progress_section(panels: list[dict]) -> None:
    print("Shared Progress")
    for line in format_shared_progress_lines(panels):
        print(line)


def _format_timeline(timeline) -> None:
    print("Timeline")
    if not timeline:
        print("- 暂无事件")
    for item in timeline:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {item.title}")


def _format_suggested_actions(suggested_actions: list) -> None:
    print("Suggested Actions")
    if suggested_actions:
        for item in suggested_actions:
            print(f"- {item}")
        return
    print("- 暂无，当前没有明显需要立刻处理的事项。")
