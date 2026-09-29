# LLM: "这个会话任务还有没有后续工作"的唯一判定入口，只读结构化事实：活跃 Goal、指向该任务的待处理 guidance、
#   未终态子代理、指向该任务的待处理唤醒（不含定时触发自身）、尚未发出完成通知的受管后台命令、启用的进度策略。
#   任何一项读取失败都记为 follow_up_unreadable（fail closed：当成还有后续工作，宁可继续等，也不误结算健康任务）。
#   不读回复正文、任务目标或模型话术。调用方（定时任务收口与 waiting 对账）只能凭返回的事实码决定是否进 waiting。
#   改动时联查 scheduler/service.py、conversation/task_promotion.complete_current_conversation_task 与
#   test_scheduler_waiting_deadlock。
# 模块用途: 判断一个会话任务在本轮结束后是否还有会自行推进的后续工作，决定定时执行该等待还是该结算。
from __future__ import annotations

from collections.abc import Callable, Iterable

FOLLOW_UP_ACTIVE_GOAL = "active_goal"
FOLLOW_UP_PENDING_GUIDANCE = "pending_guidance"
FOLLOW_UP_OPEN_SUBAGENTS = "open_subagents"
FOLLOW_UP_PENDING_WAKES = "pending_wakes"
FOLLOW_UP_PENDING_PROCESS = "pending_process_completion"
FOLLOW_UP_ENABLED_POLICY = "enabled_progress_policy"
FOLLOW_UP_UNREADABLE = "follow_up_unreadable"
# 定时触发本身不是后续工作：它只是这一轮的入口，处理完就会被确认。
_SCHEDULER_TRIGGER_REASON = "scheduled_job_due"


# LLM: 逐项检查，返回存在的事实码（按固定顺序，去重）；空元组表示确认没有任何后续工作。
#   ignore_wake_ids 用来排除调用方正在处理的唤醒自身。读取失败的那一项记 FOLLOW_UP_UNREADABLE，其余项照常检查。
# 函数用途: 列出一个会话任务当前仍存在的后续工作事实。
def task_follow_up_facts(
    agent: object, thread_id: str, task_id: str, ignore_wake_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    store = getattr(agent, "conversation_store", None)
    selected = str(task_id or "").strip()
    if store is None or not selected:
        return (FOLLOW_UP_UNREADABLE,)
    ignored = frozenset(str(item) for item in ignore_wake_ids if str(item or "").strip())
    checks: tuple[tuple[str, Callable[[], bool]], ...] = (
        (FOLLOW_UP_ACTIVE_GOAL, lambda: _has_active_goal(store, thread_id, selected)),
        (FOLLOW_UP_PENDING_GUIDANCE, lambda: bool(store.guidance.pending("task", selected, limit=1))),
        (FOLLOW_UP_OPEN_SUBAGENTS, lambda: _has_open_subagents(agent, selected)),
        (FOLLOW_UP_PENDING_WAKES, lambda: _has_pending_wakes(store, selected, ignored)),
        (FOLLOW_UP_PENDING_PROCESS, lambda: _has_pending_process(agent, selected)),
        (FOLLOW_UP_ENABLED_POLICY, lambda: _has_enabled_policy(store, selected)),
    )
    facts: list[str] = []
    for code, check in checks:
        try:
            present = check()
        except Exception:  # noqa: BLE001 - 读不到就当仍有后续工作，不能误结算
            present, code = True, FOLLOW_UP_UNREADABLE
        if present and code not in facts:
            facts.append(code)
    return tuple(facts)


# LLM: 只有 active 的 Goal 会自己产生续跑唤醒；暂停、受阻、额度或预算受限的 Goal 需要人来推动，不算后续工作。
# 函数用途: 判断任务是否挂着一个活跃的持续目标。
def _has_active_goal(store: object, thread_id: str, task_id: str) -> bool:
    goal = store.goals.load(thread_id, task_id=task_id)
    return goal is not None and str(getattr(goal, "status", "") or "") == "active"


# LLM: 复用会话任务收尾的同一条判据（canonical 子代理状态 + 精确请求血缘），读失败时它本身已 fail closed 返回 True。
# 函数用途: 判断任务是否还有未终态的子代理。
def _has_open_subagents(agent: object, task_id: str) -> bool:
    from .task_promotion import _conversation_task_has_open_subagents

    return _conversation_task_has_open_subagents(agent, task_id)


# LLM: 读取全部待处理唤醒，有读取错误就抛出（由调用方记为 unreadable）；排除调用方正在处理的唤醒和定时触发本身。
# 函数用途: 判断是否还有指向该任务、尚未处理的唤醒（子代理生命周期、后台命令完成、Goal 续跑等）。
def _has_pending_wakes(store: object, task_id: str, ignored: frozenset[str]) -> bool:
    signals, errors = store.wakes.pending_report(limit=0)
    if errors:
        raise RuntimeError("pending wake queue is unreadable")
    return any(
        str(getattr(signal, "root_task_id", "") or "").strip() == task_id
        and str(getattr(signal, "wake_signal_id", "") or "") not in ignored
        and str(getattr(signal, "reason", "") or "").strip().lower() != _SCHEDULER_TRIGGER_REASON
        for signal in signals
    )


# LLM: 后台命令的完成义务由 process_events 的权威记录判定；读取失败时它抛出，由调用方记为 unreadable。
# 函数用途: 判断任务是否还有尚未发出完成通知的受管后台命令。
def _has_pending_process(agent: object, task_id: str) -> bool:
    from .process_events import task_has_pending_process_completion

    return task_has_pending_process_completion(agent, task_id)


# LLM: 只看 enabled 的进度策略；有读取错误就抛出。
# 函数用途: 判断任务是否还有启用中的进度策略会定期唤醒它。
def _has_enabled_policy(store: object, task_id: str) -> bool:
    policies, errors = store.progress.list_report(enabled_only=True)
    if errors:
        raise RuntimeError("progress policies are unreadable")
    return any(str(getattr(policy, "task_id", "") or "").strip() == task_id for policy in policies)


__all__ = [
    "FOLLOW_UP_ACTIVE_GOAL",
    "FOLLOW_UP_ENABLED_POLICY",
    "FOLLOW_UP_OPEN_SUBAGENTS",
    "FOLLOW_UP_PENDING_GUIDANCE",
    "FOLLOW_UP_PENDING_PROCESS",
    "FOLLOW_UP_PENDING_WAKES",
    "FOLLOW_UP_UNREADABLE",
    "task_follow_up_facts",
]
