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
from ..conversation.goal_prompting import objective_updated_prompt
from ..conversation.goal_runtime import raise_goal_continuation_wake
from ..conversation.named_work import stop_named_conversation_work


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
    if operation == "clear" and request.command.name and not request.command.name.startswith("goal-"):
        return _clear_named_goal(request)
    with request.store.goal_transition_guard(thread_id):
        goals = request.store.load_goals(thread_id)
        if operation == "view":
            return _goal_view_result(goals)
        if operation == "create":
            return _create_goal(request)
        current, selection_error = _selected_goal(goals, request.command.name)
        if selection_error:
            return ConversationControlResult("goal", False, selection_error)
        if current is None:
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


# LLM: One Goal overlays the selected agent task; naming never starts a second executor beside an ordinary active turn.
# 函数用途: 给当前工作建立持续目标；空闲时建立一个根任务，已有未结束目标则提示修改。
def _create_goal(request: GoalControlRequest) -> ConversationControlResult:
    scope = request.scope
    if any(goal.status != "complete" for goal in request.store.load_goals(request.thread.thread_id)):
        return ConversationControlResult("goal", False, "当前代理已有未结束目标；请修改它或先完成/清除。", error_code="GOAL_STATE_CONFLICT")
    from .control_service import _active_conversation_task

    active = _active_conversation_task(request.owner_agent, scope, ordinary_only=True)
    task_id = str(active.payload.get("id") or "") if active is not None else ""
    try:
        goal = request.store.create_goal(
            {
                "thread_id": request.thread.thread_id,
                "objective": request.command.value,
                "name": request.command.name,
                **({"task_id": task_id} if task_id else {}),
                "duration_seconds": request.command.duration_seconds,
                "metadata": {
                    "channel": str(getattr(scope, "channel", "") or ""),
                    "conversation_id": str(getattr(scope, "conversation_id", "") or ""),
                    "created_by": str(getattr(scope, "user_id", "") or ""),
                },
            }
        )
    except ValueError as exc:
        return ConversationControlResult("goal", False, str(exc), error_code="GOAL_INVALID_REQUEST")
    request.store.bind_task(
        {
            "thread_id": request.thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
            "work_kind": "goal",
            "work_name": goal.name,
            "duration_seconds": goal.duration_seconds,
            "cancellation_scope": "foreground",
        }
    )
    request.owner_agent.local_store.task_registry.register_task(
        goal.task_id,
        status="running",
        goal=goal.objective,
        user_id=str(getattr(scope, "user_id", "") or ""),
    )
    _raise_goal_wake(request.store, goal, scope)
    return ConversationControlResult(
        "goal",
        True,
        (
            f"Goal“{goal.name}”已开始。"
            if goal.name
            else f"持续目标已开始：{goal.objective}"
        ),
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
            "status": current.status,
            "expected_status": current.status,
        }
    )
    if updated is None:
        return _goal_race_result()
    request.store.update_task_goal({"task_id": current.task_id, "goal": request.command.value})
    request.owner_agent.local_store.task_registry.update_task_description(
        current.task_id,
        request.command.value,
    )
    request.store.append_guidance(
        {
            "target_type": "task",
            "target_id": current.task_id,
            "message": objective_updated_prompt(updated),
            "sender": "goal",
            "priority": "urgent",
            "metadata": {"goal_id": updated.goal_id, "kind": "goal_objective_updated"},
        }
    )
    if updated.status == "active":
        _raise_goal_wake(request.store, updated, request.scope)
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
    if current.status != "active":
        return ConversationControlResult(
            "goal", False, f"当前目标状态为 {current.status}，不能暂停。", request_id=current.task_id
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


# LLM: 精确显式 resume 可修复旧共享任务绑定；一般目标保持原身份，active 也需核对任务和续跑事件。
# 函数用途: 恢复持久目标；不会因表面 active 就跳过已丢失的任务接续，也不会偷偷挑选多目标。
def _resume_goal(request: GoalControlRequest, current: object) -> ConversationControlResult:
    if current.status not in {"active", "paused", "blocked", "usage_limited"}:
        return ConversationControlResult(
            "goal", False, f"当前目标状态为 {current.status}，不能恢复。", request_id=current.task_id
        )
    from ..conversation.goal_recovery import prepare_goal_resume

    try:
        current = prepare_goal_resume(request.owner_agent, request.store, current)
    except ValueError as exc:
        return ConversationControlResult("goal", False, str(exc), request_id=current.task_id)
    updated = _transition_goal(request, current, "active")
    if updated is None:
        return _goal_race_result()
    if updated.status != "active":
        return ConversationControlResult(
            "goal",
            False,
            "这个 Goal 的时长或预算已经用完，不能继续运行。",
            request_id=current.task_id,
        )
    request.store.update_task_status({"task_id": current.task_id, "status": "active"})
    request.resume_registry(current.task_id)
    _raise_goal_wake(request.store, updated, request.scope)
    return ConversationControlResult(
        "goal",
        True,
        "持续目标已恢复。",
        request_id=current.task_id,
    )


# LLM: Clear terminates the overlay only; ordinary transcript and task evidence remain durable.
# 函数用途: 清除持续目标并中断当前执行，但不删除普通聊天记录和已有工作。
def _clear_goal(request: GoalControlRequest, current: object) -> ConversationControlResult:
    deleted = request.store.delete_goal(
        request.thread.thread_id,
        expected_goal_id=current.goal_id,
    )
    if deleted is None:
        return _goal_race_result()
    request.interrupt_goal(deleted)
    return ConversationControlResult(
        "goal",
        True,
        "持续目标已清除；普通对话记录不受影响。",
        request_id=current.task_id,
    )


def _clear_named_goal(request: GoalControlRequest) -> ConversationControlResult:
    stopped = stop_named_conversation_work(
        request.owner_agent,
        thread_id=request.thread.thread_id,
        kind="goal",
        name=request.command.name,
    )
    if not stopped.ok:
        messages = {
            "NAMED_WORK_NOT_FOUND": "没有找到这个 Goal。",
            "NAMED_WORK_CONFLICT": "同名 Goal 状态冲突，请先用 /status 核对。",
        }
        return ConversationControlResult(
            "goal",
            False,
            messages.get(stopped.error_code, "Goal 状态暂时不可用，请稍后重试。"),
        )
    return ConversationControlResult(
        "goal",
        True,
        f"Goal“{request.command.name}”已停止。",
        request_id=stopped.task_id,
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
def _goal_view_result(goals: list[object]) -> ConversationControlResult:
    visible = [
        goal
        for goal in goals
        if str(getattr(goal, "status", "") or "") != "complete"
    ]
    if not visible:
        return ConversationControlResult("goal", True, "当前没有持续目标。")
    if len(visible) > 1:
        lines = ["当前 Goal："]
        for goal in sorted(
            visible,
            key=lambda item: float(getattr(item, "created_at", 0.0) or 0.0),
        ):
            name = str(getattr(goal, "name", "") or "").strip() or "未命名"
            status = str(getattr(goal, "status", "") or "")
            lines.append(f"- {name}｜{_GOAL_STATUS_LABELS.get(status, status)}｜{goal.goal_id}")
        lines.append("精确操作：/goal 名称或目标编号 pause|resume|clear")
        return ConversationControlResult("goal", True, "\n".join(lines))
    goal = visible[0]
    status = str(getattr(goal, "status", "") or "")
    objective = str(getattr(goal, "objective", "") or "")
    time_used = max(0, int(getattr(goal, "time_used_seconds", 0) or 0))
    tokens_used = max(0, int(getattr(goal, "tokens_used", 0) or 0))
    token_budget = getattr(goal, "token_budget", None)
    name = str(getattr(goal, "name", "") or "").strip()
    lines = [f"持续目标：{objective}", f"状态：{_GOAL_STATUS_LABELS.get(status, status)}", f"编号：{goal.goal_id}"]
    if name:
        lines.insert(0, f"名称：{name}")
    if time_used:
        lines.append(f"已用时间：{time_used} 秒")
    if token_budget is not None:
        lines.append(f"Token：{tokens_used}/{int(token_budget)}")
    return ConversationControlResult(
        "goal",
        True,
        "\n".join(lines),
        request_id=str(getattr(goal, "task_id", "") or ""),
    )


_GOAL_STATUS_LABELS = {
    "active": "运行中",
    "paused": "已暂停",
    "blocked": "已阻塞",
    "usage_limited": "使用额度受限",
    "budget_limited": "目标预算已用完",
    "complete": "已完成",
}


# LLM: 选择仅来自当前 thread 的精确编号或名称，含糊时返回错误，不按更新时间猜。
# 函数用途: 从当前目标中选中用户要操作的一条；旧无名冲突也可通过编号处理。
def _selected_goal(goals: list[object], name: str) -> tuple[object | None, str]:
    unfinished = [
        goal
        for goal in goals
        if str(getattr(goal, "status", "") or "") != "complete"
    ]
    if name:
        matching = [
            goal
            for goal in unfinished
            if goal.goal_id == name or str(getattr(goal, "name", "") or "").casefold() == name.casefold()
        ]
        if len(matching) > 1:
            return None, "同名 Goal 状态冲突，请先用 /status 核对。"
        return (matching[0], "") if matching else (None, "")
    if len(unfinished) > 1:
        return None, "当前有多个 Goal，请用 /goal 名称或目标编号 pause|resume|clear 指定一个。"
    return (unfinished[0], "") if unfinished else (None, "")


# LLM: Wake identity is the persisted goal/task/thread tuple; dedupe by goal id across retries.
# 函数用途: 为持续目标写入下一轮自动推进信号。
def _raise_goal_wake(store: object, goal: object, scope: object) -> None:
    raise_goal_continuation_wake(
        store,
        goal,
        channel=str(getattr(scope, "channel", "") or ""),
        conversation_id=str(getattr(scope, "conversation_id", "") or ""),
    )


__all__ = ["GoalControlRequest", "execute_goal_control_operation"]
