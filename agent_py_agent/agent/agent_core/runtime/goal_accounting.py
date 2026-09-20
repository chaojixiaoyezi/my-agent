# LLM: 计费只认当前 run 的 thread/task/goal 结构化绑定；多个目标共存时不得把同会话用量记入兄弟目标。
# 活跃秒数由 store.goal_clock 的同源共享对象结算，持久用量仍在原 Goal 事务中保存。
# 模块用途: 在模型请求和回合边界记录目标用量，保留缓存与预算口径。
from __future__ import annotations

"""Persist 会话运行时 goal usage at live turn boundaries."""

from ...conversation.goal_binding import goal_binding
from ..model.usage import goal_token_usage


# LLM: 请求前按 exact goal/task 读取目标并启动进程内时钟，不授予新的运行权限。
# 函数用途: 为本轮模型调用开始目标计时，普通无目标任务不受影响。
def begin_goal_model_turn(agent: object, params: object) -> object | None:
    """Start the live wall-clock baseline before a provider request."""
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return None
    thread_id, task_id, goal_id, _ = goal_binding(agent, params)
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None:
        return None
    try:
        goal = store.goals.load(thread_id, goal_id=goal_id, task_id=task_id)
    except KeyError:
        return None
    if (
        goal is None
        or goal.task_id != task_id
        or goal.status not in {"active", "budget_limited"}
    ):
        return None
    store.goal_clock.begin(goal)
    return goal


# LLM: 在目标锁中按 exact goal/task 写入服务商用量；预算到限只注入原有系统状态，不改用户目标。
# 函数用途: 结算本次模型调用的目标花费，避免多目标之间串账。
def account_goal_model_response(agent: object, params: object, response: object) -> object | None:
    """Charge non-cached input + output and live wall time to the exact bound goal."""
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return None
    thread_id, task_id, goal_id, _ = goal_binding(agent, params)
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None:
        return None
    try:
        with store.goals.transition_guard(thread_id):
            goal = store.goals.load(thread_id, goal_id=goal_id, task_id=task_id)
            if (
                goal is None
                or goal.task_id != task_id
                or goal.status not in {"active", "budget_limited"}
            ):
                return None
            elapsed = store.goal_clock.take_elapsed_seconds(goal)
            updated = store.goals.account_usage(
                {
                    "thread_id": thread_id,
                    "goal_id": goal.goal_id,
                    "token_delta": goal_token_usage(response),
                    "time_delta_seconds": elapsed,
                    "mode": "active_only",
                }
            )
    except KeyError:
        # Some callers inject a durable task identity from a different store instance.
        # Goal accounting is optional unless that exact thread exists in this store.
        return None
    if updated is not None and updated.status == "budget_limited":
        _inject_budget_limit_once(params, updated)
    return updated


# LLM: 只清理当前绑定目标的进程内时钟；回合结束本身不改变持久 Goal 状态。
# 函数用途: 目标暂停或完成时收起计时，兄弟目标继续独立计量。
def finish_goal_turn_accounting(agent: object, task_attributes: object) -> None:
    """Clear the live clock when a turn ends with a non-active goal."""
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    from types import SimpleNamespace

    thread_id, task_id, goal_id, _ = goal_binding(agent, SimpleNamespace(task_attributes=attrs))
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None:
        return
    try:
        goal = store.goals.load(thread_id, goal_id=goal_id, task_id=task_id)
    except KeyError:
        return
    if goal is not None and goal.task_id == task_id and goal.status != "active":
        store.goal_clock.clear(thread_id, goal_id=goal.goal_id)


def _inject_budget_limit_once(params: object, goal: object) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict) or state.get("goal_budget_limit_injected") is True:
        return
    from ...conversation.goal_prompting import budget_limit_prompt

    injections = getattr(params, "runtime_injections", None)
    if isinstance(injections, list):
        injections.append(budget_limit_prompt(goal))
        state["goal_budget_limit_injected"] = True


__all__ = [
    "account_goal_model_response",
    "begin_goal_model_turn",
    "finish_goal_turn_accounting",
]
