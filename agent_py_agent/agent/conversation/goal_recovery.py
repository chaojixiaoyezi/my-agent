# LLM: 仅显式目标控制调用此模块；读取或启动不迁移。共享任务必须已完成且无执行者，迁移不重放历史操作。
# 模块用途: 安全修复升级前多个 Goal 误绑同一任务的旧数据，保留原现场并拒绝活动执行中的改绑。
from __future__ import annotations


# LLM: 调用者持有 goal transition guard；running claim 即使过期也不等于原执行结束，须由执行器先释放。
# 函数用途: 用户精确恢复旧目标时分离冲突任务身份；正常目标原样返回。
def prepare_goal_resume(agent: object, store: object, goal: object) -> object:
    siblings = store.load_goals(goal.thread_id)
    shared = any(item.goal_id != goal.goal_id and item.task_id == goal.task_id for item in siblings)
    if not shared:
        return goal
    from .run_claim import detached_task_claim_scope_id
    from .runtime import _goal_subagent_phase

    with store.task_transition_guard(goal.task_id):
        link = store.load_task_link(goal.task_id)
        if link is None or link.thread_id != goal.thread_id or link.status != "completed":
            raise ValueError("旧目标共享任务尚未确认完成；请先处理原执行状态，不能直接改绑重跑。")
        phase, state_error = _goal_subagent_phase(agent, goal.task_id)
        if state_error or phase == "subagents_active":
            raise ValueError("旧目标任务仍有未结束子代理或状态不可读；保留现场，暂不迁移。")
        for scope in ("", detached_task_claim_scope_id(goal.thread_id, goal.task_id)):
            claim = store.load_background_run_claim(goal.thread_id, claim_scope_id=scope)
            if claim.get("load_error") or claim.get("status") == "running":
                raise ValueError("会话执行尚未释放；请等当前回合结束后恢复这个 Goal。")
        task_id = f"goal-task-{goal.goal_id.removeprefix('goal-')}"
        if task_id == goal.task_id:
            raise ValueError("目标编号与旧共享任务仍冲突，需检查原始记录，未做更改。")
        prepared = store.load_task_link(task_id)
        if prepared is not None and (
            prepared.thread_id != goal.thread_id or prepared.status != "interrupted"
            or prepared.task_path != link.task_path or prepared.goal != goal.objective
        ):
            raise ValueError("目标的迁移位置已被占用；未覆盖现有任务。")
        store.bind_task({
            "thread_id": goal.thread_id, "task_id": task_id, "goal": goal.objective,
            "task_path": link.task_path, "status": "interrupted", "work_kind": "goal",
            "work_name": goal.name, "duration_seconds": goal.duration_seconds,
            "cancellation_scope": "detached" if goal.name else "foreground",
            "context_anchor_message_id": link.context_anchor_message_id,
        })
        migrated = store.rebind_goal_task(
            goal.thread_id, goal_id=goal.goal_id, expected_task_id=goal.task_id, task_id=task_id,
        )
        if migrated is None:
            raise ValueError("目标刚刚被其他操作修改，已保留现场，请重新查看 Goal。")
        return migrated
