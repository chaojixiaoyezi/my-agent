# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

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


# LLM: _format_takeover_view_section exposes concrete recovery entries for parent takeover.
# 函数用途: 在 status 输出里列出可接管 run 和推荐读取 refs，不读取 artifact 正文。
def _format_takeover_view_section(panels: list[dict]) -> None:
    print("Takeover View")
    for line in format_takeover_view_lines(panels):
        print(line)


# LLM: StatusPrintContext 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
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


# LLM: _GatewaySectionRequest 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass
class _GatewaySectionRequest:
    status: str
    pid: int | None
    alive: bool
    heartbeat_age: float
    paths: Any


# LLM: print_status_human 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def print_status_human(ctx: StatusPrintContext):
    agent = ctx.agent
    print("MY-AGENT STATUS")
    print(f"agent={agent.config.agent_name}")
    print(f"workspace={agent.root}")
    print("")
    _format_gateway_section(
        _GatewaySectionRequest(ctx.gateway_status, ctx.pid, ctx.alive, ctx.heartbeat_age, ctx.paths),
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


# LLM: _format_gateway_section 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _format_gateway_section(request: _GatewaySectionRequest, request_counts: dict) -> None:
    print("Gateway")
    print(f"- status={request.status} pid={request.pid if request.pid else '-'} alive={request.alive}")
    if request.heartbeat_age:
        print(f"- heartbeat_age_seconds={request.heartbeat_age:.1f}")
    print("- requests=" + json.dumps(request_counts, ensure_ascii=False, sort_keys=True))
    print(f"- workspace={request.paths.root}")


# LLM: _format_active_work_block 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _format_active_work_block(active_work_summary) -> None:
    if active_work_summary:
        _format_active_work(active_work_summary)
        return
    print("进行中任务")
    print("- 暂无")


# LLM: _format_active_work 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _format_active_work(active_work_summary) -> None:
    from ..agent.startup_recovery import format_active_work_summary

    print("进行中任务")
    print("-" + format_active_work_summary(active_work_summary).replace("\n", "\n  - "))
    if active_work_summary.active_task_count > 0:
        print("  运行 my-agent subagents-dispatch 可继续调度")


# LLM: _format_subagents_section 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
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


# LLM: _format_shared_progress_section surfaces refs-only control-plane panels in status output.
# 函数用途: 展示共享进度和 failure handoff 引用数量，不读取 artifact 正文。
def _format_shared_progress_section(panels: list[dict]) -> None:
    print("Shared Progress")
    for line in format_shared_progress_lines(panels):
        print(line)


# LLM: _format_timeline 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _format_timeline(timeline) -> None:
    print("Timeline")
    if not timeline:
        print("- 暂无事件")
    for item in timeline:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {item.title}")


# LLM: _format_suggested_actions 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _format_suggested_actions(suggested_actions: list) -> None:
    print("Suggested Actions")
    if suggested_actions:
        for item in suggested_actions:
            print(f"- {item}")
        return
    print("- 暂无，当前没有明显需要立刻处理的事项。")
