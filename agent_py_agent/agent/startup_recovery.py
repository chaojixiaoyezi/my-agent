
# LLM: This legacy-named module is now a read-only runtime-status projection. TUI startup must
# never call it, and recovery mutations belong to the single Gateway's structured controllers.
# 模块用途: 为显式 status 命令汇总 Gateway、队列和子代理运行事实；只读，不负责恢复或改状态。
"""运行状态检测功能。

检测当前环境中的未收口工作，包括：
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


# LLM: Keep this DTO presentation-only; no field may authorize lifecycle mutations.
# 类用途: 承载一次 status 快照，供文本和 JSON 状态页复用。
@dataclass
class ActiveWorkSummary:
    """未收口工作摘要。"""

    gateway_alive: bool = False
    gateway_pid: int = 0
    active_task_count: int = 0
    stale_request_count: int = 0
    recent_tasks: list[dict] = None
    processing_requests: list[str] = None
    dispatch_pending: bool = False
    dispatch_rounds: int = 0
    orphan_processes: list[dict] = None
    crashed_tasks: list[dict] = None  # RUNNING 且结构化 runner 会话已失联的任务
    detection_errors: list[dict] = None

    def __post_init__(self) -> None:
        if self.recent_tasks is None:
            self.recent_tasks = []
        if self.processing_requests is None:
            self.processing_requests = []
        if self.orphan_processes is None:
            self.orphan_processes = []
        if self.crashed_tasks is None:
            self.crashed_tasks = []
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


# 孤儿进程身份只认真实派工子命令。不能拿 ``agent_py_agent`` 包名作 marker：
# 从该目录启动的任意 Python 进程，其解释器路径就可能包含这个字符串，导致 pid
# 复用保护误报。后台派工的 canonical argv 必含 ``subagents-dispatch``。
_ORPHAN_CMDLINE_MARKERS = ("subagents-dispatch",)


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


# LLM: Status may flag only a stale canonical runner session, never infer a task crash from the
# shared background-dispatch PID. Lifecycle repair is deliberately left to Gateway supervision.
# 函数用途: 找出状态仍为 RUNNING、心跳已过期且 runner 宿主已经退出或属于旧 Gateway 的任务。
def _detect_crashed_running_tasks(agent, summary) -> None:
    from .contracts.state_machine import normalize_status
    from .subagents.process_control import is_pid_alive
    from .subagents.runner_session_liveness import (
        has_fresh_runner_session,
        runner_session_of,
    )

    try:
        tasks = list(agent.subagents.list_runs() or [])
    except Exception as exc:
        _append_detection_error(summary, exc, "startup_recovery.crashed_tasks")
        return
    for task in tasks:
        if normalize_status(getattr(task, "status", "")) != TaskStatus.RUNNING.value:
            continue
        session = runner_session_of(task)
        session_status = str(session.get("status") or "").strip().lower()
        if session_status not in {"starting", "running"} or has_fresh_runner_session(task):
            continue
        try:
            worker_pid = int(session.get("worker_pid") or 0)
        except (TypeError, ValueError):
            worker_pid = 0
        old_gateway_generation = bool(session.get("in_process")) and bool(
            summary.gateway_pid and worker_pid != summary.gateway_pid
        )
        if not old_gateway_generation and (worker_pid <= 0 or is_pid_alive(worker_pid)):
            continue
        summary.crashed_tasks.append(
            {
                "run_id": str(getattr(task, "id", "") or ""),
                "pid": worker_pid,
                "status": str(getattr(task, "status", "") or ""),
                "session_id": str(session.get("session_id") or ""),
                "reason": (
                    "gateway_generation_changed"
                    if old_gateway_generation
                    else "runner_process_exited"
                ),
            }
        )


# 函数用途: 读进程命令行(ps 跨 darwin/linux;读不到返回空串=身份验证不过,不报)。
def _process_cmdline(pid: int) -> str:
    import subprocess

    try:
        # -ww 禁用按列宽截断:无终端环境(CI / daemon / systemd 服务,COLUMNS 未设)
        # 下 ps 默认把 command 列截到 ~80 字符,会把孤儿身份特征
        # (subagents-dispatch / agent_py_agent)截掉 → 真实孤儿漏报。my-agent 正以
        # 后台 daemon 形态运行(无终端),这是生产可观测性缺陷,非仅测试问题。
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
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


# LLM: This function is a pure observation boundary. Never call lifecycle setters, stale-attempt
# recovery, dispatch, or process termination from status collection.
# 函数用途: 读取当前运行事实并生成状态快照；调用它不会恢复任务、改账或启动进程。
def detect_active_work(agent: SimpleAgent) -> ActiveWorkSummary:
    from .gateway_parts import gateway_paths

    paths = gateway_paths(agent)
    summary = ActiveWorkSummary()

    _detect_gateway_state(paths, summary)
    _detect_processing_requests(paths, summary)
    _detect_active_tasks(agent, summary)
    _detect_dispatch_status(agent, summary)
    _detect_orphan_processes(agent, summary)
    _detect_crashed_running_tasks(agent, summary)

    return summary


def _render_orphan_processes(summary: ActiveWorkSummary) -> list[str]:
    """渲染孤儿派工进程段(任务已终态但进程仍活)。无则空。从 format 抽出以控行数。"""
    if not summary.orphan_processes:
        return []
    lines = [f"⚠ 发现 {len(summary.orphan_processes)} 个孤儿派工进程（任务已终态但进程仍在运行，疑似上次异常退出遗留）"]
    for orphan in summary.orphan_processes[:3]:
        lines.append(f"  - pid={orphan['pid']} run={orphan['run_id']} status={orphan['task_status']}")
    lines.append("  确认后可手动终止（kill <pid>），系统不会自动回收。")
    return lines


# LLM: Render diagnostic facts and the owning recovery component only; never advertise a config
# switch that mutates tasks from a status/TUI process.
# 函数用途: 展示已失联 runner，并告诉用户这类恢复由 Gateway 后台负责。
def _render_crashed_tasks(summary: ActiveWorkSummary) -> list[str]:
    if not summary.crashed_tasks:
        return []
    lines = [f"⚠ 发现 {len(summary.crashed_tasks)} 个 RUNNING 任务的 runner 会话已失联"]
    for crashed in summary.crashed_tasks[:3]:
        lines.append(
            f"  - run={crashed['run_id']} status={crashed['status']} "
            f"worker_pid={crashed['pid']} reason={crashed.get('reason', '')}"
        )
    lines.append("  单 Gateway 会按 runner 会话、心跳和 attempt 围栏自动调和；status 本身不会改任务状态。")
    return lines


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
        lines.append(f"✓ 发现 {summary.active_task_count} 个近期未收口任务（含运行、等待或阻塞）")
    else:
        lines.append("✓ 没有近期未收口任务")

    if summary.stale_request_count > 0:
        lines.append(f"⚠ 发现 {summary.stale_request_count} 个遗留的 processing 请求")
        if summary.processing_requests:
            lines.append(f"  请求 IDs: {', '.join(summary.processing_requests[:3])}")

    if summary.dispatch_pending:
        lines.append(f"⚠ 有未完成的 dispatch 循环（已运行 {summary.dispatch_rounds} 轮）")
        lines.append("  是否继续？使用 my-agent daemon --continue 继续调度")

    lines.extend(_render_orphan_processes(summary))
    lines.extend(_render_crashed_tasks(summary))

    if summary.detection_errors:
        lines.append(f"⚠ 状态读取有 {len(summary.detection_errors)} 个错误")
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

    return "\n".join(lines) if lines else "当前没有未收口任务。"


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
        or bool(summary.crashed_tasks)
    )


__all__ = [
    "ActiveWorkSummary",
    "detect_active_work",
    "format_active_work_summary",
    "has_active_work",
    "is_recent_board_item",
]
