# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

"""启动时恢复检测功能。

检测当前环境中的进行中任务，包括：
- gateway 是否存活
- 未完成的子代理任务
- 遗留的 processing 请求
"""
# LLM: 这里只汇总恢复提示，不应制造新的副作用或改变任务状态。
# 模块用途: 启动时检测 gateway、processing 请求、子代理任务、通知和 dispatch 遗留状态。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .subagents.models import SubAgentBoardOptions

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: ActiveWorkSummary 属于 兼容入口 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 启动恢复检测结果，汇总仍在进行或需要提示用户的工作。
@dataclass
class ActiveWorkSummary:
    """进行中任务摘要。"""

    gateway_alive: bool = False
    gateway_pid: int = 0
    active_task_count: int = 0
    stale_request_count: int = 0
    recent_tasks: list[dict] = None
    processing_requests: list[str] = None
    pending_notifications: int = 0
    dispatch_pending: bool = False
    dispatch_rounds: int = 0

    # LLM: ActiveWorkSummary.__post_init__ 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 补齐 dataclass 的派生默认值，避免调用方处理 None。
    def __post_init__(self) -> None:
        if self.recent_tasks is None:
            self.recent_tasks = []
        if self.processing_requests is None:
            self.processing_requests = []


# LLM: _detect_gateway_state 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 读取 gateway 进程状态并写入启动恢复摘要。
def _detect_gateway_state(paths, summary):
    """检测 gateway 状态。"""
    from .gateway import gateway_running
    pid, alive = gateway_running(paths)
    summary.gateway_alive = alive
    summary.gateway_pid = pid


# LLM: _detect_processing_requests 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 统计遗留 processing 请求并记录请求 id。
def _detect_processing_requests(paths, summary):
    """获取 processing 请求列表。"""
    from .gateway import gateway_request_counts
    request_counts = gateway_request_counts(paths)
    summary.stale_request_count = request_counts.get("processing", 0)
    if paths.processing.exists():
        summary.processing_requests = [f.stem for f in paths.processing.glob("*.json")]


# LLM: _detect_active_tasks 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 读取子代理看板，汇总未完成任务数和最近任务。
def _detect_active_tasks(agent, summary):
    """检测未完成的子代理任务和最近任务列表。"""
    try:
        board = agent.subagents.build_board(options=SubAgentBoardOptions(recent_limit=3))
        final_statuses = {"DONE", "FAILED", "CANCELLED", "TIMEOUT"}
        active_tasks = [item for item in board.hot_list if item.status not in final_statuses]
        summary.active_task_count = len(active_tasks)
        summary.recent_tasks = [
            {
                "id": item.id,
                "goal": item.goal,
                "status": item.status,
                "verification_status": item.verification_status,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in board.recent[:3]
        ]
    except (AttributeError, TypeError):
        summary.active_task_count = 0
        summary.recent_tasks = []


# LLM: _detect_pending_notifications 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 统计当前用户仍未处理的通知数量。
def _detect_pending_notifications(agent, summary):
    """检测未读通知。"""
    try:
        from .notification import NotificationManager
        if agent.config.notification_enabled:
            notif_manager = NotificationManager(agent.config)
            summary.pending_notifications = notif_manager.get_pending_count(agent.config.user_id)
    except Exception:
        summary.pending_notifications = 0


# LLM: _detect_dispatch_status 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 读取代理内存中的 dispatch 循环状态。
def _detect_dispatch_status(agent, summary):
    """检测未完成的 dispatch 循环状态。"""
    try:
        has_pending = getattr(agent, "_has_pending_work", False)
        rounds = getattr(agent, "_consecutive_dispatch_rounds", 0)
        summary.dispatch_pending = has_pending and rounds > 0
        summary.dispatch_rounds = rounds
    except Exception:
        summary.dispatch_pending = False
        summary.dispatch_rounds = 0


# LLM: detect_active_work 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 集中执行启动恢复探测并返回 ActiveWorkSummary。
def detect_active_work(agent: SimpleAgent) -> ActiveWorkSummary:
    """检测当前环境中的进行中任务。

    Args:
        agent: SimpleAgent 实例

    Returns:
        ActiveWorkSummary 包含 gateway 状态、活跃任务数、遗留请求数和最近任务列表
    """
    from .gateway import gateway_paths

    paths = gateway_paths(agent)
    summary = ActiveWorkSummary()

    _detect_gateway_state(paths, summary)
    _detect_processing_requests(paths, summary)
    _detect_active_tasks(agent, summary)
    _detect_pending_notifications(agent, summary)
    _detect_dispatch_status(agent, summary)

    return summary


# LLM: format_active_work_summary 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把恢复摘要渲染成 CLI 可展示的中文提示文本。
def format_active_work_summary(summary: ActiveWorkSummary) -> str:
    """格式化进行中任务摘要为可读文本。

    Args:
        summary: ActiveWorkSummary 实例

    Returns:
        格式化的摘要文本
    """
    lines = []

    if summary.gateway_alive:
        lines.append(f"✓ Gateway 运行中 (pid={summary.gateway_pid})")
    else:
        lines.append("✗ Gateway 未运行")

    if summary.active_task_count > 0:
        lines.append(f"✓ 发现 {summary.active_task_count} 个进行中任务")
    else:
        lines.append("✓ 没有进行中任务")

    if summary.stale_request_count > 0:
        lines.append(f"⚠ 发现 {summary.stale_request_count} 个遗留的 processing 请求")
        if summary.processing_requests:
            lines.append(f"  请求 IDs: {', '.join(summary.processing_requests[:3])}")

    if summary.pending_notifications > 0:
        lines.append(f"📬 有 {summary.pending_notifications} 条未读通知")
        lines.append("  运行 my-agent notifications 查看详情")

    if summary.dispatch_pending:
        lines.append(f"⚠ 有未完成的 dispatch 循环（已运行 {summary.dispatch_rounds} 轮）")
        lines.append("  是否继续？使用 my-agent daemon --continue 继续调度")

    if summary.recent_tasks:
        lines.append("最近任务:")
        for task in summary.recent_tasks:
            flags = f"{task['status']}/{task['verification_status']}"
            lines.append(f"  - {task['id']} {flags} :: {task['goal'][:60]}...")

    return "\n".join(lines) if lines else "当前没有进行中任务。"


# LLM: has_active_work 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 判断恢复摘要中是否存在需要用户注意的未完成工作。
def has_active_work(summary: ActiveWorkSummary) -> bool:
    """判断是否有进行中任务。

    Args:
        summary: ActiveWorkSummary 实例

    Returns:
        如果有任何进行中任务返回 True
    """
    return (
        summary.active_task_count > 0
        or summary.stale_request_count > 0
        or summary.dispatch_pending
    )


__all__ = [
    "ActiveWorkSummary",
    "detect_active_work",
    "format_active_work_summary",
    "has_active_work",
]
