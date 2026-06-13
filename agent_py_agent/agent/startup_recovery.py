
"""启动时恢复检测功能。

检测当前环境中的进行中任务，包括：
- gateway 是否存活
- 未完成的子代理任务
- 遗留的 processing 请求
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .runtime_errors import runtime_error_report
from .subagents.models import SubAgentBoardOptions, TaskStatus, task_status_in

if TYPE_CHECKING:
    from ..core import SimpleAgent

_ACTIVE_WORK_RECENT_SECONDS = 72 * 60 * 60
_RECENT_TASK_GOAL_PREVIEW_CHARS = 160


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
    orphan_processes: list[dict] = None
    detection_errors: list[dict] = None

    def __post_init__(self) -> None:
        if self.recent_tasks is None:
            self.recent_tasks = []
        if self.processing_requests is None:
            self.processing_requests = []
        if self.orphan_processes is None:
            self.orphan_processes = []
        if self.detection_errors is None:
            self.detection_errors = []


def _detect_gateway_state(paths, summary):
    """检测 gateway 状态。"""
    from .gateway_parts import gateway_running
    pid, alive = gateway_running(paths)
    summary.gateway_alive = alive
    summary.gateway_pid = pid


def _detect_processing_requests(paths, summary):
    """获取 processing 请求列表。"""
    from .gateway_parts import gateway_request_counts
    request_counts = gateway_request_counts(paths)
    summary.stale_request_count = request_counts.get("processing", 0)
    if paths.processing.exists():
        summary.processing_requests = [f.stem for f in paths.processing.glob("*.json")]


def _detect_active_tasks(agent, summary):
    """检测未完成的子代理任务和最近任务列表。"""
    try:
        board = agent.subagents.board.build_board(
            options=SubAgentBoardOptions(
                recent_limit=3,
                include_child_status_counts=False,
            )
        )
        final_statuses = frozenset({
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.TIMEOUT.value,
        })
        now = time.time()
        active_tasks = [
            item for item in board.hot_list
            if not task_status_in(item.status, final_statuses) and is_recent_board_item(item, now=now)
        ]
        summary.active_task_count = len(active_tasks)
        summary.recent_tasks = [
            {
                "id": item.id,
                "goal": _task_goal_preview(item.goal),
                "goal_truncated": _goal_is_truncated(item.goal),
                "status": item.status,
                "verification_status": item.verification_status,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in board.recent[:3]
        ]
    except (AttributeError, TypeError) as exc:
        summary.active_task_count = 0
        summary.recent_tasks = []
        _append_detection_error(summary, exc, "startup_recovery.active_tasks")


def _detect_pending_notifications(agent, summary):
    """检测未读通知。"""
    try:
        from .notification import NotificationManager
        if agent.config.notification_enabled:
            notif_manager = NotificationManager(agent.config)
            summary.pending_notifications = notif_manager.get_pending_count(agent.config.user_id)
    except Exception as exc:
        summary.pending_notifications = 0
        _append_detection_error(summary, exc, "startup_recovery.pending_notifications")


def _detect_dispatch_status(agent, summary):
    """检测未完成的 dispatch 循环状态。"""
    try:
        has_pending = getattr(agent, "_has_pending_work", False)
        rounds = getattr(agent, "_consecutive_dispatch_rounds", 0)
        summary.dispatch_pending = has_pending and rounds > 0
        summary.dispatch_rounds = rounds
    except Exception as exc:
        summary.dispatch_pending = False
        summary.dispatch_rounds = 0
        _append_detection_error(summary, exc, "startup_recovery.dispatch_status")


def _append_detection_error(summary, exc: BaseException, context: str) -> None:
    summary.detection_errors.append(runtime_error_report(exc, context=context))


# 孤儿进程身份特征:cmdline 必须含任一特征才认作本系统的后台派工进程
# (通道运行时"kill 前验证进程身份"同款,防 pid 复用误报别人家进程)。
_ORPHAN_CMDLINE_MARKERS = ("agent_py_agent", "subagents-dispatch")


# LLM: 启动时孤儿进程检测(REFACTORING_BACKLOG"启动时孤儿进程检测",孤儿回收
#   第二期)。出口回收只覆盖结构化退出点;run 进程异常崩溃(kill -9/断电)时后台
#   dispatch 进程漏网。本检测=observability 先行:扫描任务落盘的
#   background_start.pid(权威事实源,CLI 抹 pid 缺陷已修),进程活着 + cmdline
#   含本系统特征 + 任务已终态 → 报告为孤儿;**绝不自动杀**(回收命令提示用户)。
#   非终态任务的活进程视为可能在干活,不报。任何读取异常记 detection_errors。
# 函数用途: 开机巡检"有没有上次崩溃留下的、还在后台空转的派工进程"。
def _detect_orphan_processes(agent, summary) -> None:
    from .contracts.state_machine import TERMINAL_STATES, normalize_status
    from .subagents.process_control import is_pid_alive

    try:
        tasks = list(agent.subagents.list_runs() or [])
    except Exception as exc:
        _append_detection_error(summary, exc, "startup_recovery.orphan_processes")
        return
    seen_pids: set[int] = set()
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        background = attrs.get("background_start") if isinstance(attrs, dict) else None
        if not isinstance(background, dict):
            continue
        try:
            pid = int(background.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in seen_pids:
            continue
        if normalize_status(getattr(task, "status", "")) not in TERMINAL_STATES:
            continue
        if not is_pid_alive(pid):
            continue
        cmdline = _process_cmdline(pid)
        if not any(marker in cmdline for marker in _ORPHAN_CMDLINE_MARKERS):
            continue
        seen_pids.add(pid)
        summary.orphan_processes.append(
            {
                "pid": pid,
                "launch_id": str(background.get("launch_id") or ""),
                "run_id": str(getattr(task, "id", "") or ""),
                "task_status": str(getattr(task, "status", "") or ""),
                "cmdline_preview": cmdline[:160],
            }
        )


# 函数用途: 读进程命令行(ps 跨 darwin/linux;读不到返回空串=身份验证不过,不报)。
def _process_cmdline(pid: int) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip()


def is_recent_board_item(item, *, now: float) -> bool:
    progress_age = _float_attr(item, "seconds_since_progress")
    if progress_age > _ACTIVE_WORK_RECENT_SECONDS:
        return False
    updated_at = _float_attr(item, "updated_at")
    return not updated_at or now - updated_at <= _ACTIVE_WORK_RECENT_SECONDS


def _float_attr(item, name: str) -> float:
    try:
        return float(getattr(item, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _task_goal_preview(goal: object) -> str:
    text = str(goal or "").replace("\n", " ").strip()
    if len(text) <= _RECENT_TASK_GOAL_PREVIEW_CHARS:
        return text
    return text[:_RECENT_TASK_GOAL_PREVIEW_CHARS].rstrip() + "..."


def _goal_is_truncated(goal: object) -> bool:
    return len(str(goal or "").replace("\n", " ").strip()) > _RECENT_TASK_GOAL_PREVIEW_CHARS


def detect_active_work(agent: SimpleAgent) -> ActiveWorkSummary:
    """检测当前环境中的进行中任务。

    Args:
        agent: SimpleAgent 实例

    Returns:
        ActiveWorkSummary 包含 gateway 状态、活跃任务数、遗留请求数和最近任务列表
    """
    from .gateway_parts import gateway_paths

    paths = gateway_paths(agent)
    summary = ActiveWorkSummary()

    _detect_gateway_state(paths, summary)
    _detect_processing_requests(paths, summary)
    _detect_active_tasks(agent, summary)
    _detect_pending_notifications(agent, summary)
    _detect_dispatch_status(agent, summary)
    _detect_orphan_processes(agent, summary)

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

    if summary.orphan_processes:
        lines.append(f"⚠ 发现 {len(summary.orphan_processes)} 个孤儿派工进程（任务已终态但进程仍在运行，疑似上次异常退出遗留）")
        for orphan in summary.orphan_processes[:3]:
            lines.append(f"  - pid={orphan['pid']} run={orphan['run_id']} status={orphan['task_status']}")
        lines.append("  确认后可手动终止（kill <pid>），系统不会自动回收。")

    if summary.detection_errors:
        lines.append(f"⚠ 启动恢复检测有 {len(summary.detection_errors)} 个读取错误")
        for error in summary.detection_errors[:3]:
            lines.append(
                f"  - {error.get('context')}: {error.get('category')} "
                f"{error.get('error_type')} :: {error.get('message')}"
            )

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
        or summary.dispatch_pending
        or bool(summary.orphan_processes)
    )


__all__ = [
    "ActiveWorkSummary",
    "detect_active_work",
    "format_active_work_summary",
    "has_active_work",
    "is_recent_board_item",
]
