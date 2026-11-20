# LLM: 后台 Goal 的状态裁决只读原领域账本；异常结算保留同一 transition_guard、CAS 和写入顺序。
# 不持有 Agent、完整 Store、调度器或租约；调用方在原唤醒确认位置调用，联测 Goal、取消和恢复合同。
# 模块用途: 处理后台目标异常停住及正常回合后的续跑，沿既有发布入口安排下一轮。
from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .goal_progress_fuse import (
    frozen_goal_disposition,
    goal_report_reason_fields,
    slice_progress_evidence,
)
from .models import BackgroundMainAgentReport, WakeSignal

if TYPE_CHECKING:
    from .goal_clock import GoalClockGroup
    from .store_goals import GoalStore
    from .store_tasks import TaskStore


_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")


# LLM: 仅注入 Goal/任务/时钟领域及若干精确能力；回调在原分支内查询，不预取子树或注册表。
#   user_turn_gate 按 thread_id 持有车道闸并给出“用户回合在场”（run_claim.user_input_turn_gate），熔断判定与落账都在闸内；
#   缺省 None 表示不判断（与 C5 之前相同）。queue_withheld_notice(goal, signal, report) 在续跑资格被结束原因扣下时
#   排队（并对可外发通道主动推一次）宿主提示；缺省 None 不提示。
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
    goal_continuation_idle_limit: int = 3
    queue_no_progress_notice: Callable[[object], object] | None = None
    user_turn_gate: Callable[[str], AbstractContextManager[bool]] | None = None
    queue_withheld_notice: Callable[[object, WakeSignal, BackgroundMainAgentReport], object] | None = None


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


# 每次重试先读取原唤醒的已记账裁决，投递失败也不会再次判定或重复累计。
def _continue_goal_after_report_once(
    dependencies: GoalContinuationDependencies,
    signal: WakeSignal,
    *,
    report: BackgroundMainAgentReport,
    now: float,
) -> None:
    goal = _goal_for_signal(dependencies, signal)
    if goal is None:
        return
    frozen = frozen_goal_disposition(goal, signal.wake_signal_id)
    if frozen is not None:
        if (frozen.get("action") in {"continue", "withheld"} and _matches_disposition(goal, frozen)
                and dependencies.task_status(signal.thread_id, goal.task_id) == "active"):
            _apply_disposition(dependencies, signal, goal, report, frozen, now=now)
        return
    if goal.status != "active":
        if report.goal_continuation_allowed and goal.status in {"blocked", "usage_limited", "budget_limited"}:
            dependencies.tasks.update_status({"task_id": goal.task_id, "status": "interrupted"})
        return
    task_status = dependencies.task_status(signal.thread_id, goal.task_id)
    if task_status == "" and report.goal_continuation_allowed:
        # 原任务读取偶发失败时仍在同一轮复读一次。
        task_status = dependencies.task_status(signal.thread_id, goal.task_id)
    if task_status != "active":
        return
    _account_goal_slice(dependencies, signal, report, _GoalSlice(goal, task_status, now))


def continue_goal_after_report(
    dependencies: GoalContinuationDependencies,
    signal: WakeSignal,
    *,
    report: BackgroundMainAgentReport,
    now: float,
) -> None:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    goal_id = str(metadata.get("goal_id") or "").strip()
    try:
        for attempt in range(2):
            try:
                _continue_goal_after_report_once(dependencies, signal, report=report, now=now)
                return
            except Exception as exc:
                if attempt == 1:
                    raise
                _LOGGER.warning(
                    "thread goal continuation attempt failed; retrying once code=%s goal_id=%s "
                    "effect_outcome=%s next_action=%s error=%s",
                    "GOAL_CONTINUATION_RETRYING",
                    goal_id,
                    "retrying_after_lock_unwind",
                    "retry idempotent thread-goal continuation once",
                    exc,
                )
    except Exception as exc:
        _LOGGER.warning(
            "thread goal continuation failed code=%s effect_outcome=%s next_action=%s error=%s",
            "GOAL_CONTINUATION_WAKE_FAILED",
            "goal_continuation_wake_not_raised",
            "inspect conversation store health or retry continuation wake on next tick",
            exc,
            exc_info=True,
        )


@dataclass(frozen=True)
class _GoalSlice:
    goal: object
    task_status: str
    now: float


def _goal_for_signal(dependencies: GoalContinuationDependencies, signal: WakeSignal) -> object | None:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    goal_id = str(metadata.get("goal_id") or "").strip()
    goal = dependencies.goals.load(signal.thread_id, goal_id=goal_id)
    if goal is None or goal.goal_id != goal_id or goal.task_id != str(signal.root_task_id or ""):
        return None
    return goal


def _disposition_action(
    dependencies: GoalContinuationDependencies, goal: object, report: BackgroundMainAgentReport,
) -> tuple[str, str]:
    from ..turn_end import TurnEndReason, normalize_turn_end_reason
    from .technical_continuation import USER_INPUT_YIELD_REASON

    allowed = report.goal_continuation_allowed
    if not allowed:
        if str(report.runtime_reason or "").strip().upper() == USER_INPUT_YIELD_REASON:
            return "deferred", "user_input_yield"
        if normalize_turn_end_reason(report.turn_end_reason) == TurnEndReason.ABORTED.value:
            return "deferred", "user_stop"
    try:
        phase, state_error = dependencies.subagent_phase(goal.task_id)
    except Exception as exc:
        if not allowed:
            _LOGGER.warning(
                "thread goal subagent state unavailable code=%s effect_outcome=%s next_action=%s error=%s",
                "GOAL_CONTINUATION_WITHHELD_NOTICE_FAILED", "notice_not_queued",
                "inspect subagent state before resuming the goal", exc,
            )
            return "deferred", "subagent_state_unknown"
        _LOGGER.warning(
            "goal subagent phase check failed; continuing with idle fuse code=%s task_id=%s "
            "effect_outcome=%s next_action=%s error=%s",
            "SUBAGENT_STATE_UNKNOWN_CONTINUE", goal.task_id, "continuing_goal_slice",
            "verify subagent store readability on next scheduler tick", exc,
        )
        return "continue", "subagent_state_unknown"
    if phase == "subagents_active":
        return "deferred", "subagents_active"
    if allowed:
        return "continue", "report_allows_continuation"
    if state_error:
        return "deferred", "subagent_state_unknown"
    return "withheld", "report_withholds_continuation"


def _account_goal_slice(
    dependencies: GoalContinuationDependencies, signal: WakeSignal, report: BackgroundMainAgentReport, facts: _GoalSlice,
) -> None:
    """以同一次车道闸和 Goal 原子更新记下裁决及熔断事实。"""
    goal = facts.goal
    action, basis = _disposition_action(dependencies, goal, report)
    evidence = (
        slice_progress_evidence(signal, report, goal, facts.task_status) if report.goal_slice_facts_known
        else {"progressed": "unknown", "basis": "goal_slice_facts_unavailable"}
    )
    progressed = evidence["progressed"]
    gate = dependencies.user_turn_gate if action != "deferred" else None
    with gate(goal.thread_id) if gate is not None else nullcontext(False) as user_turn_present:
        if user_turn_present and action == "withheld":
            action, basis = "deferred", "user_turn_present"
        count_progress = action == "continue" and type(progressed) is bool and (progressed or not user_turn_present)
        disposition = {
            "action": action,
            "basis": basis,
            "goal_revision": int(getattr(goal, "revision", 0) or 0),
            "progress_evidence": evidence,
            "report_facts": goal_report_reason_fields(report),
        }
        updated, tripped = dependencies.goals.record_continuation_fuse({
            "thread_id": goal.thread_id,
            "goal_id": goal.goal_id,
            "task_id": goal.task_id,
            "wake_signal_id": signal.wake_signal_id,
            "progressed": progressed is True,
            "count_progress": count_progress,
            "idle_limit": dependencies.goal_continuation_idle_limit,
            "now": facts.now,
            "disposition": disposition,
        })
    if updated is None:
        return
    if tripped:
        notify = dependencies.queue_no_progress_notice
        if notify is not None and notify(updated) is False:
            _LOGGER.warning("thread goal no-progress notice was not queued(goal=%s)", updated.goal_id)
        return
    committed = frozen_goal_disposition(updated, signal.wake_signal_id)
    _apply_disposition(dependencies, signal, updated, report, committed or disposition, now=facts.now)


def _matches_disposition(goal: object, disposition: dict) -> bool:
    return goal.status == "active" and int(getattr(goal, "revision", 0) or 0) == disposition.get("goal_revision")


def _apply_disposition(
    dependencies: GoalContinuationDependencies, signal: WakeSignal, goal: object,
    report: BackgroundMainAgentReport, disposition: dict, *, now: float,
) -> None:
    if not _matches_disposition(goal, disposition):
        return
    action = disposition.get("action")
    if action == "continue":
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        dependencies.raise_wake(
            goal, channel=str(metadata.get("channel") or ""),
            conversation_id=str(metadata.get("conversation_id") or ""), now=now,
        )
    elif action == "withheld" and dependencies.queue_withheld_notice is not None:
        evidence = disposition["progress_evidence"]
        fields = {
            **goal_report_reason_fields(disposition["report_facts"]),
            "goal_continuation_allowed": False,
            "goal_slice_facts_known": type(evidence["progressed"]) is bool,
            "tool_call_count": evidence.get("tool_call_count", 0),
            "tool_success_count": evidence.get("tool_success_count", 0),
            "material_progress_count": evidence.get("material_progress_count", 0),
        }
        frozen_report = report if all(getattr(report, key) == value for key, value in fields.items()) else replace(report, **fields)
        try:
            if dependencies.queue_withheld_notice(goal, signal, frozen_report) is False:
                _LOGGER.warning("thread goal withheld-continuation notice was not queued(goal=%s)", goal.goal_id)
        except Exception as exc:
            _LOGGER.warning(
                "thread goal withheld-continuation notice failed code=%s effect_outcome=%s next_action=%s error=%s",
                "GOAL_CONTINUATION_WITHHELD_NOTICE_FAILED", "notice_not_queued",
                "inspect conversation store health; the goal stays active without a continuation wake", exc,
            )
