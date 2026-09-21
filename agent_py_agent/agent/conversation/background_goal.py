# LLM: 后台 Goal 的状态裁决只读原领域账本；异常结算保留同一 transition_guard、CAS 和写入顺序。
# 不持有 Agent、完整 Store、调度器或租约；调用方在原唤醒确认位置调用，联测 Goal、取消和恢复合同。
# 模块用途: 处理后台目标异常停住及正常回合后的续跑，沿既有发布入口安排下一轮。
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import BackgroundMainAgentReport, WakeSignal

if TYPE_CHECKING:
    from .goal_clock import GoalClockGroup
    from .store_goals import GoalStore
    from .store_tasks import TaskStore


_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")


# LLM: 仅注入 Goal/任务/时钟领域及四个精确能力；回调在原分支内查询，不预取子树或注册表。
# 类用途: 列明目标续跑需要的依赖，不缓存目标状态、不创建第二份账本或后台工作。
@dataclass(frozen=True)
class GoalContinuationDependencies:
    goals: GoalStore
    tasks: TaskStore
    goal_clock: GoalClockGroup
    task_status: Callable[[str, str], str]
    subagent_phase: Callable[[str], tuple[str, str]]
    raise_wake: Callable[..., object]
    task_registry: Callable[[], object | None]


# LLM: 只更新 wake 精确匹配的 active Goal；原事务内先结算时钟、再 CAS 停目标、改任务并登记注册表。
# 函数用途: 后台轮出现原调用方分类的错误时停住同一目标，保留原持久记录和异常传播边界。
def stop_goal_after_error(
    dependencies: GoalContinuationDependencies,
    signal: WakeSignal,
    *,
    status: str,
) -> None:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    with dependencies.goals.transition_guard(signal.thread_id):
        goal = dependencies.goals.load(
            signal.thread_id,
            goal_id=str(metadata.get("goal_id") or "").strip(),
        )
        if (
            goal is None
            or goal.status != "active"
            or goal.goal_id != str(metadata.get("goal_id") or "")
            or goal.task_id != str(signal.root_task_id or "")
        ):
            return
        elapsed = dependencies.goal_clock.take_elapsed_seconds(goal)
        if elapsed:
            goal = (
                dependencies.goals.account_usage(
                    {
                        "thread_id": goal.thread_id,
                        "goal_id": goal.goal_id,
                        "time_delta_seconds": elapsed,
                        "mode": "active_only",
                    }
                )
                or goal
            )
        updated = dependencies.goals.update(
            {
                "thread_id": goal.thread_id,
                "goal_id": goal.goal_id,
                "status": status,
                "expected_status": "active",
            }
        )
        if updated is None:
            return
        dependencies.tasks.update_status({"task_id": goal.task_id, "status": "interrupted"})
        registry = dependencies.task_registry()
        if registry is not None:
            registry.register_task(goal.task_id, status="blocked", goal=updated.objective)


# LLM: 精确 Goal 和任务状态决定续跑；子树仍活动或未知时由原生命周期事件接续，不另建轮询。
# 函数用途: 一轮报告确认后同步受阻任务状态，或经原去重发布能力安排下一轮；失败仍只记原警告。
def continue_goal_after_report(
    dependencies: GoalContinuationDependencies,
    signal: WakeSignal,
    *,
    report: BackgroundMainAgentReport,
    now: float,
) -> None:
    if not report.goal_continuation_allowed:
        return
    try:
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        goal = dependencies.goals.load(
            signal.thread_id,
            goal_id=str(metadata.get("goal_id") or "").strip(),
        )
        if (
            goal is None
            or goal.goal_id != str(metadata.get("goal_id") or "")
            or goal.task_id != str(signal.root_task_id or "")
        ):
            return
        task_status = dependencies.task_status(signal.thread_id, goal.task_id)
        if goal.status != "active":
            if goal.status in {"blocked", "usage_limited", "budget_limited"}:
                dependencies.tasks.update_status(
                    {"task_id": goal.task_id, "status": "interrupted"}
                )
            return
        if task_status != "active":
            return
        subagent_phase, state_error = dependencies.subagent_phase(goal.task_id)
        if subagent_phase == "subagents_active" or state_error:
            return
        dependencies.raise_wake(
            goal,
            channel=str(metadata.get("channel") or ""),
            conversation_id=str(metadata.get("conversation_id") or ""),
            now=now,
        )
    except Exception:
        _LOGGER.warning("thread goal continuation failed", exc_info=True)
