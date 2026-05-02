"""启动时恢复检测功能。

检测当前环境中的进行中任务，包括：
- gateway 是否存活
- 未完成的子代理任务
- 遗留的 processing 请求
"""
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass
class ActiveWorkSummary:
    """进行中任务摘要。"""

    gateway_alive: bool = False
    gateway_pid: int = 0
    active_task_count: int = 0
    stale_request_count: int = 0
    recent_tasks: list[dict] = None
    processing_requests: list[str] = None

    def __post_init__(self) -> None:
        if self.recent_tasks is None:
            self.recent_tasks = []
        if self.processing_requests is None:
            self.processing_requests = []


def detect_active_work(agent: SimpleAgent) -> ActiveWorkSummary:
    """检测当前环境中的进行中任务。

    Args:
        agent: SimpleAgent 实例

    Returns:
        ActiveWorkSummary 包含 gateway 状态、活跃任务数、遗留请求数和最近任务列表
    """
    from .gateway import gateway_paths, gateway_running, gateway_request_counts
    from .gateway_parts.process_control import is_pid_alive

    paths = gateway_paths(agent)
    summary = ActiveWorkSummary()

    # 1. 检测 gateway 状态
    pid, alive = gateway_running(paths)
    summary.gateway_alive = alive
    summary.gateway_pid = pid

    # 2. 检测 gateway 请求统计
    request_counts = gateway_request_counts(paths)
    summary.stale_request_count = request_counts.get("processing", 0)

    # 3. 获取 processing 请求列表
    if paths.processing.exists():
        processing_files = list(paths.processing.glob("*.json"))
        summary.processing_requests = [f.stem for f in processing_files]

    # 4. 检测未完成的子代理任务
    try:
        board = agent.subagents.build_board(recent_limit=3)
        # 统计非最终状态的任务（不是 DONE、FAILED、CANCELLED）
        final_statuses = {"DONE", "FAILED", "CANCELLED", "TIMEOUT"}
        active_tasks = [
            item for item in board.hot_list
            if item.status not in final_statuses
        ]
        summary.active_task_count = len(active_tasks)

        # 5. 获取最近的任务列表
        summary.recent_tasks = []
        for item in board.recent[:3]:
            summary.recent_tasks.append({
                "id": item.id,
                "goal": item.goal,
                "status": item.status,
                "verification_status": item.verification_status,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            })
    except (AttributeError, TypeError):
        # 处理 board 为 None 或无效的情况
        summary.active_task_count = 0
        summary.recent_tasks = []

    return summary


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
        lines.append(f"✗ Gateway 未运行")

    if summary.active_task_count > 0:
        lines.append(f"✓ 发现 {summary.active_task_count} 个进行中任务")
    else:
        lines.append("✓ 没有进行中任务")

    if summary.stale_request_count > 0:
        lines.append(f"⚠ 发现 {summary.stale_request_count} 个遗留的 processing 请求")
        if summary.processing_requests:
            lines.append(f"  请求 IDs: {', '.join(summary.processing_requests[:3])}")

    if summary.recent_tasks:
        lines.append("最近任务:")
        for task in summary.recent_tasks:
            flags = f"{task['status']}/{task['verification_status']}"
            lines.append(f"  - {task['id']} {flags} :: {task['goal'][:60]}...")

    return "\n".join(lines) if lines else "当前没有进行中任务。"


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
    )


__all__ = [
    "ActiveWorkSummary",
    "detect_active_work",
    "format_active_work_summary",
    "has_active_work",
]
