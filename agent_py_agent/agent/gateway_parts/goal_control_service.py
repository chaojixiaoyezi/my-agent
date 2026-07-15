from __future__ import annotations

"""Persistent `/goal` lifecycle operations for one exact owner conversation.

LLM: This module owns goal lifecycle semantics only; adapter routing and generic task control stay in
``control_service``. Goal mutations must remain owner/thread scoped and transition-guarded.

模块用途: 集中处理持续目标的查看、创建、修改、暂停、恢复和清除，避免 Gateway 主控制器长成一大段分支。
"""

from collections.abc import Callable
from dataclasses import dataclass

from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
)


@dataclass(frozen=True)
# LLM: Carries resolved owner/thread authority plus runtime side-effect callbacks; never resolve from prose here.
# 类用途: 把一次 /goal 操作需要的可信会话、存储和中断能力装成一个请求对象。
class GoalControlRequest:
    owner_agent: object
    store: object
    thread: object
    command: ConversationControlCommand
    scope: object
    interrupt_goal: Callable[[object], None]
    resume_registry: Callable[[str], None]


# LLM: One transition lock covers load plus mutation; handlers may not introduce a second goal authority.
# 函数用途: 在当前会话的目标锁内分派并执行一条已解析的 /goal 操作。
def execute_goal_control_operation(request: GoalControlRequest) -> ConversationControlResult:
    """Apply one parsed `/goal` operation under the thread's goal transition lock."""
    thread_id = str(getattr(request.thread, "thread_id", "") or "")
    operation = request.command.operation or "view"
    with request.store.goal_transition_guard(thread_id):
        current = request.store.load_goal(thread_id)
        if operation == "view":
            return _goal_view_result(current)
        if operation == "create":
            return _create_goal(request, current)
        if current is None or current.status in {"complete", "cleared"}:
            return ConversationControlResult("goal", False, "当前没有可操作的持续目标。")
        handlers = {
            "edit": _edit_goal,
            "pause": _pause_goal,
            "resume": _resume_goal,
            "clear": _clear_goal,
        }
        handler = handlers.get(operation)
        if handler is None:
            return ConversationControlResult(
                "goal",
                False,
                request.command.usage or "不支持的 /goal 操作。",
            )
        return handler(request, current)


# LLM: Enforces one unfinished goal per thread and binds the goal to the durable root task before waking it.
# 函数用途: 新建持续目标、登记根任务并发布第一次续跑信号。
def _create_goal(request: GoalControlRequest, current: object | None) -> ConversationControlResult:
    if current is not None and current.status in {"active", "paused", "blocked"}:
        return ConversationControlResult(
            "goal",
            False,
            "当前已有未结束的目标；请先完成或使用 /goal clear。",
            request_id=current.task_id,
        )
    scope = request.scope
    goal = request.store.create_goal(
        {
            "thread_id": request.thread.thread_id,
            "objective": request.command.value,
            "metadata": {
                "channel": str(getattr(scope, "channel", "") or ""),
                "conversation_id": str(getattr(scope, "conversation_id", "") or ""),
                "created_by": str(getattr(scope, "user_id", "") or ""),
            },
        }
    )
    request.store.bind_task(
        {
            "thread_id": request.thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    request.owner_agent.local_store.task_registry.register_task(
        goal.task_id,
        status="running",
        goal=goal.objective,
        user_id=str(getattr(scope, "user_id", "") or ""),
    )
    _raise_goal_wake(request.store, goal, scope, reason="thread_goal_continue")
    return ConversationControlResult(
        "goal",
        True,
        f"持续目标已开始：{goal.objective}",
        request_id=goal.task_id,
    )


# LLM: Edits objective text without changing goal/task identity or workspace lineage.
# 函数用途: 修改当前持续目标的内容，并同步任务索引中的显示说明。
def _edit_goal(request: GoalControlRequest, current: object) -> ConversationControlResult:
    updated = request.store.update_goal(
        {
            "thread_id": request.thread.thread_id,
            "goal_id": current.goal_id,
            "objective": request.command.value,
        }
    )
    if updated is None:
        return _goal_race_result()
    request.store.update_task_goal({"task_id": current.task_id, "goal": request.command.value})
    request.owner_agent.local_store.task_registry.update_task_description(
        current.task_id,
        request.command.value,
    )
    return ConversationControlResult(
        "goal",
        True,
        f"持续目标已修改：{request.command.value}",
        request_id=current.task_id,
    )


# LLM: Pause preserves transcript/task files but interrupts the currently executing turn and descendants.
# 函数用途: 暂停持续目标，保留现场，同时通知运行时停止当前执行。
def _pause_goal(request: GoalControlRequest, current: object) -> ConversationControlResult:
    if current.status == "paused":
        return ConversationControlResult(
            "goal",
            True,
            "持续目标已经暂停。",
            request_id=current.task_id,
        )
    updated = _transition_goal(request, current, "paused")
    if updated is None:
        return _goal_race_result()
    request.interrupt_goal(updated)
    return ConversationControlResult(
        "goal",
        True,
        "持续目标已暂停；对话和已有工作记录仍保留。",
        request_id=current.task_id,
    )


# LLM: Resume reactivates the same task identity and publishes one deduplicated continuation wake.
# 函数用途: 恢复原持续目标和原任务，不创建新会话或新工作区。
def _resume_goal(request: GoalControlRequest, current: object) -> ConversationControlResult:
    if current.status == "active":
        return ConversationControlResult(
            "goal",
            True,
            "持续目标正在运行。",
            request_id=current.task_id,
        )
    updated = _transition_goal(request, current, "active")
    if updated is None:
        return _goal_race_result()
    request.store.update_task_status({"task_id": current.task_id, "status": "active"})
    request.resume_registry(current.task_id)
    _raise_goal_wake(request.store, updated, request.scope, reason="thread_goal_continue")
    return ConversationControlResult(
        "goal",
        True,
        "持续目标已恢复。",
        request_id=current.task_id,
    )


# LLM: Clear terminates the overlay only; ordinary transcript and task evidence remain durable.
# 函数用途: 清除持续目标并中断当前执行，但不删除普通聊天记录和已有工作。
def _clear_goal(request: GoalControlRequest, current: object) -> ConversationControlResult:
    updated = _transition_goal(request, current, "cleared")
    if updated is None:
        return _goal_race_result()
    request.interrupt_goal(updated)
    return ConversationControlResult(
        "goal",
        True,
        "持续目标已清除；普通对话记录不受影响。",
        request_id=current.task_id,
    )


# LLM: Goal status changes use goal-id plus expected-status CAS to reject stale controllers.
# 函数用途: 用比较后更新方式安全切换当前目标状态。
def _transition_goal(request: GoalControlRequest, current: object, status: str):
    return request.store.update_goal(
        {
            "thread_id": request.thread.thread_id,
            "goal_id": current.goal_id,
            "status": status,
            "expected_status": current.status,
        }
    )


# LLM: Concurrent transition conflicts are explicit and never silently retried against a newer goal.
# 函数用途: 返回目标刚被其他执行修改时的统一提示。
def _goal_race_result() -> ConversationControlResult:
    return ConversationControlResult("goal", False, "目标刚刚发生变化，请重试。")


# LLM: View renders only public goal fields and never leaks workspace paths or internal wake records.
# 函数用途: 把当前持续目标整理成用户可读的状态文字。
def _goal_view_result(goal: object | None) -> ConversationControlResult:
    if goal is None or str(getattr(goal, "status", "") or "") == "cleared":
        return ConversationControlResult("goal", True, "当前没有持续目标。")
    labels = {
        "active": "运行中",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "complete": "已完成",
    }
    status = str(getattr(goal, "status", "") or "")
    objective = str(getattr(goal, "objective", "") or "")
    count = int(getattr(goal, "continuation_count", 0) or 0)
    return ConversationControlResult(
        "goal",
        True,
        f"持续目标：{objective}\n状态：{labels.get(status, status)}\n已续跑：{count} 次",
        request_id=str(getattr(goal, "task_id", "") or ""),
    )


# LLM: Wake identity is the persisted goal/task/thread tuple; dedupe by goal id across retries.
# 函数用途: 为持续目标写入下一轮自动推进信号。
def _raise_goal_wake(store: object, goal: object, scope: object, *, reason: str) -> None:
    store.raise_wake_signal(
        {
            "thread_id": str(getattr(goal, "thread_id", "") or ""),
            "root_task_id": str(getattr(goal, "task_id", "") or ""),
            "urgency": "normal",
            "reason": reason,
            "summary": "继续推进当前持续目标。",
            "dedupe_key": f"thread-goal:{str(getattr(goal, 'goal_id', '') or '')}",
            "metadata": {
                "goal_id": str(getattr(goal, "goal_id", "") or ""),
                "channel": str(getattr(scope, "channel", "") or ""),
                "conversation_id": str(getattr(scope, "conversation_id", "") or ""),
            },
        }
    )


__all__ = ["GoalControlRequest", "execute_goal_control_operation"]
