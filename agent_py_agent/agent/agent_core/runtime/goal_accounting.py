from __future__ import annotations

"""Persist 会话运行时 goal usage at live turn boundaries."""

from ..model.usage import goal_token_usage


def begin_goal_model_turn(agent: object, params: object) -> object | None:
    """Start the live wall-clock baseline before a provider request."""
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return None
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None:
        return None
    try:
        goal = store.load_goal(thread_id)
    except KeyError:
        return None
    if (
        goal is None
        or goal.task_id != task_id
        or goal.status not in {"active", "budget_limited"}
    ):
        return None
    store.begin_goal_accounting(goal)
    return goal


def account_goal_model_response(agent: object, params: object, response: object) -> object | None:
    """Charge non-cached input + output and live wall time to the exact bound goal."""
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return None
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None:
        return None
    try:
        with store.goal_transition_guard(thread_id):
            goal = store.load_goal(thread_id)
            if (
                goal is None
                or goal.task_id != task_id
                or goal.status not in {"active", "budget_limited"}
            ):
                return None
            elapsed = store.take_goal_elapsed_seconds(goal)
            updated = store.account_goal_usage(
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


def finish_goal_turn_accounting(agent: object, task_attributes: object) -> None:
    """Clear the live clock when a turn ends with a non-active goal."""
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None:
        return
    try:
        goal = store.load_goal(thread_id)
    except KeyError:
        return
    if goal is not None and goal.task_id == task_id and goal.status != "active":
        store.clear_goal_accounting(thread_id, goal_id=goal.goal_id)


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
