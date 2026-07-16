from __future__ import annotations

"""Single wake authority for continuing one persisted thread goal."""


def raise_goal_continuation_wake(
    store: object,
    goal: object,
    *,
    channel: str = "",
    conversation_id: str = "",
    now: float | None = None,
) -> object:
    request: dict[str, object] = {
        "thread_id": str(getattr(goal, "thread_id", "") or ""),
        "root_task_id": str(getattr(goal, "task_id", "") or ""),
        "urgency": "normal",
        "reason": "thread_goal_continue",
        "summary": "Continue working toward the active thread goal.",
        "dedupe_key": f"thread-goal:{str(getattr(goal, 'goal_id', '') or '')}",
        "metadata": {
            "goal_id": str(getattr(goal, "goal_id", "") or ""),
            "channel": str(channel or ""),
            "conversation_id": str(conversation_id or ""),
        },
    }
    if now is not None:
        request["now"] = now
    return store.raise_wake_signal(request)


def schedule_goal_activated_in_turn(agent: object, task_attributes: object) -> bool:
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    if attrs.get("thread_goal_activation_pending") is not True:
        return False
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    goal_id = str(attrs.get("thread_goal_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not goal_id or store is None:
        return False
    with store.goal_transition_guard(thread_id):
        goal = store.load_goal(thread_id)
        if goal is None or goal.goal_id != goal_id or goal.status != "active":
            return False
        raise_goal_continuation_wake(store, goal)
    attrs.pop("thread_goal_activation_pending", None)
    return True


__all__ = ["raise_goal_continuation_wake", "schedule_goal_activated_in_turn"]
