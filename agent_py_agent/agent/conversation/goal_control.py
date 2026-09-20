# LLM: Adapter-neutral authenticated Goal view/edit. Resolve the caller's thread or an authorized descendant, never trust client thread IDs.
# 模块用途: TUI/Web 复用的目标编辑入口；用户只能查看和修改自己当前会话树内的目标。
from __future__ import annotations

from .agent_control import AgentControlError, _authorized_agent_target
from .goal_editing import save_goal_objective
from .goal_runtime import raise_goal_continuation_wake


# LLM: Only view/save operations; edit does not resume paused agents, create goals, or grant execution outside the existing tree.
# 函数用途: 读取完整目标或保存用户明确确认的草稿，过期版本返回冲突，普通查询不产生写入。
def execute_agent_goal_control(agent: object, *, scope: object, payload: dict) -> dict:
    run_id = str(payload.get("run_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if run_id:
        store, _root, task = _authorized_agent_target(agent, scope=scope, run_id=run_id, operation="send_guidance")
        thread_id = str(task.agent_thread_id or "")
    else:
        thread, error = store.threads.resolve_report(
            channel=scope.channel, channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id,
        )
        if thread is None or error:
            raise AgentControlError(404, "GOAL_THREAD_NOT_FOUND", "当前会话尚未建立目标。")
        thread_id = thread.thread_id
    goal_id = str(payload.get("goal_id") or "").strip()
    if not goal_id:
        raise AgentControlError(400, "GOAL_ID_REQUIRED", "请选择准确的目标。")
    goal = store.goals.load(thread_id, goal_id=goal_id)
    if goal is None or (run_id and goal.task_id != run_id):
        raise AgentControlError(404, "GOAL_NOT_FOUND", "当前代理没有这个目标。")
    operation = str(payload.get("operation") or "view")
    if operation == "view":
        return {"ok": True, "goal": goal.public_dict()}
    if operation != "save":
        raise AgentControlError(400, "GOAL_OPERATION_INVALID", "不支持的目标操作。")
    revision = payload.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AgentControlError(400, "GOAL_REVISION_REQUIRED", "请先读取目标当前版本再保存。")
    try:
        updated = save_goal_objective(
            agent, store, goal, objective=str(payload.get("objective") or ""), expected_revision=revision, child=bool(run_id),
        )
    except ValueError as exc:
        raise AgentControlError(400, "GOAL_INVALID_OBJECTIVE", str(exc)) from exc
    if updated is None:
        raise AgentControlError(409, "GOAL_REVISION_CONFLICT", "目标已被修改或状态已变化；草稿已保留，请重新读取后比较。")
    if not run_id and updated.status == "active":
        raise_goal_continuation_wake(store, updated)
    return {"ok": True, "goal": updated.public_dict(), "message": "目标已保存；运行中的代理会在安全边界读取新要求。"}
