# LLM: Delegated goals use the existing child thread/store and runner; no new worker, timer or quality gate.
# 模块用途: 在显式派工时保存子代理的持续目标，并判断正常回合后是否仍需继续同一目标。
from __future__ import annotations

from ..concurrency.interrupt import is_interrupted
from ..turn_end import result_turn_end_reason


# LLM: Called only by create_run after child thread materialization, before launch. Prompt-only tasks have no Goal side effect.
# 函数用途: 给显式携带 persistent_goal 的新子代理建立目标，普通派工不自动转换为持续任务。
def seed_delegated_goal(manager: object, task: object) -> None:
    attrs = getattr(task, "attributes", None) or {}
    objective = str(attrs.get("persistent_goal") or "").strip()
    if not objective:
        return
    store = getattr(manager, "conversation_store", None)
    if store is None:
        raise ValueError("persistent_goal requires the child conversation store")
    if not {"get_goal", "update_goal"}.issubset(set(getattr(task, "allowed_tools", None) or [])):
        raise ValueError("persistent_goal requires get_goal and update_goal in the child tool scope")
    store.goals.create({
        "thread_id": task.agent_thread_id, "task_id": task.id, "objective": objective,
        "metadata": {"created_by_parent_run_id": task.parent_id, "agent_run_id": task.id},
    })


# LLM: 只有 active Goal 可继续；paused 只关闭续跑，不把已经正常完成的回合伪标成 interrupted。
# 函数用途: 子代理自然回复后核对续跑资格，区分目标暂停、真实执行中断和预算等限制。
def active_delegated_goal_after_turn(agent: object, task: object, result: object) -> object | None:
    if is_interrupted() or result_turn_end_reason(result) != "completed":
        return None
    goal = agent.conversation_store.goals.load(task.agent_thread_id, task_id=task.id)
    if goal is not None and goal.task_id == task.id and goal.status not in {"active", "paused", "complete"}:
        result.runtime_status = "blocked"
        result.turn_end_reason = result.runtime_status
        result.runtime_reason = f"thread_goal_{goal.status}"
    return goal if goal is not None and goal.task_id == task.id and goal.status == "active" else None


# LLM: Call only after authorized lifecycle control; this exact child CAS never changes its parent Goal or publishes another executor.
# 函数用途: 停止时暂停子目标、显式恢复运行时恢复子目标，保留目标正文、历史和用量。
def transition_delegated_goal(manager: object, task: object, *, expected_status: str, status: str) -> None:
    store = getattr(manager, "conversation_store", None)
    thread_id = str(getattr(task, "agent_thread_id", "") or "")
    if store is None or not thread_id:
        return
    with store.goals.transition_guard(thread_id):
        goal = store.goals.load(thread_id, task_id=task.id)
        if goal is None or goal.task_id != task.id or goal.status != expected_status:
            return
        store.goals.update({"thread_id": thread_id, "goal_id": goal.goal_id,
                           "expected_revision": goal.revision, "expected_status": expected_status, "status": status})
