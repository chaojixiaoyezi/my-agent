# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""JSON payload helpers for the local status command."""

from dataclasses import dataclass
from typing import Any

from .shared_progress import shared_progress_for_board


# LLM: GatewayStatusRequest 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class GatewayStatusRequest:
    alive: bool
    gateway_state: dict
    heartbeat: dict
    stale_seconds: float
    now: float


# LLM: resolve_gateway_status 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def resolve_gateway_status(request: GatewayStatusRequest) -> tuple[str, float]:
    heartbeat_at = float(request.heartbeat.get("updated_at", 0) or 0)
    heartbeat_age = request.now - heartbeat_at if heartbeat_at else 0
    state_status = request.gateway_state.get("status", "stopped")
    gateway_status = "running" if request.alive else ("stopped" if state_status == "running" else state_status)
    if request.alive and heartbeat_at and heartbeat_age > request.stale_seconds:
        gateway_status = "stale"
    return gateway_status, heartbeat_age


# LLM: StatusPayloadContext 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
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


# LLM: build_status_payload 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def build_status_payload(ctx: StatusPayloadContext) -> dict:
    return {
        "agent_name": ctx.agent.config.agent_name,
        "workspace_root": str(ctx.agent.root),
        "gateway": {
            "status": ctx.gateway_status,
            "pid": ctx.pid,
            "alive": ctx.alive,
            "heartbeat_age_seconds": round(ctx.heartbeat_age, 1) if ctx.heartbeat_age else 0,
            "request_counts": ctx.request_counts,
            "workspace": str(ctx.paths.root),
        },
        "local_store": ctx.local_stats,
        "subagents": _subagents_payload(ctx),
        "active_work": _active_work_payload(ctx.active_work_summary),
        "timeline": [item.__dict__ for item in ctx.timeline],
    }


# LLM: _active_work_payload 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _active_work_payload(active_work_summary: Any) -> dict | None:
    if not active_work_summary:
        return None
    return {
        "gateway_alive": active_work_summary.gateway_alive,
        "active_task_count": active_work_summary.active_task_count,
        "stale_request_count": active_work_summary.stale_request_count,
        "recent_tasks": active_work_summary.recent_tasks,
    }


# LLM: _subagents_payload 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_payload(ctx: StatusPayloadContext) -> dict:
    return {
        "summary": ctx.board.summary,
        "hot_count": len(ctx.board.hot_list),
        "recent_count": len(ctx.board.recent),
        "hot": [item.__dict__ for item in ctx.board.hot_list[: ctx.agent.config.subagent_board_limit]],
        "recent": [item.__dict__ for item in ctx.board.recent[: ctx.agent.config.subagent_board_limit]],
        "shared_progress": shared_progress_for_board(ctx.agent, ctx.board, purpose="status"),
    }
