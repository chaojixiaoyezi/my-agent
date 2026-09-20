# LLM: 仅显式目标控制迁移多个未结束 Goal 的共享冲突；已完成历史不是冲突。迁移仍要求任务关闭且无执行者。
# 模块用途: 安全修复旧并行目标的任务误绑定；同一项目先后建立目标不改身份，保留正常暂停恢复。
from __future__ import annotations


# LLM: 调用者持有目标锁；只有另一个未结束 Goal 共用 task 才需分离，迁移时 running claim 过期也不等于释放。
# 函数用途: 显式恢复时排除真正的旧多目标冲突，不让已完成的历史 Goal 阻止当前工作继续。
def prepare_goal_resume(agent: object, store: object, goal: object) -> object:
    siblings = store.goals.list(goal.thread_id)
    shared = any(item.goal_id != goal.goal_id and item.task_id == goal.task_id
                 and item.status != "complete" for item in siblings)
    if not shared:
        return goal
    from .run_claim import detached_task_claim_scope_id
    from .runtime import _goal_subagent_phase

    with store.tasks.transition_guard(goal.task_id):
        link = store.tasks.load(goal.task_id)
        if link is None or link.thread_id != goal.thread_id or link.status != "completed":
            raise ValueError("旧目标共享任务尚未确认完成；请先处理原执行状态，不能直接改绑重跑。")
        phase, state_error = _goal_subagent_phase(agent, goal.task_id)
        if state_error or phase == "subagents_active":
            raise ValueError("旧目标任务仍有未结束子代理或状态不可读；保留现场，暂不迁移。")
        for scope in ("", detached_task_claim_scope_id(goal.thread_id, goal.task_id)):
            claim = store.claims.load(goal.thread_id, claim_scope_id=scope)
            if claim.get("load_error") or claim.get("status") == "running":
                raise ValueError("会话执行尚未释放；请等当前回合结束后恢复这个 Goal。")
        task_id = f"goal-task-{goal.goal_id.removeprefix('goal-')}"
        if task_id == goal.task_id:
            raise ValueError("目标编号与旧共享任务仍冲突，需检查原始记录，未做更改。")
        prepared = store.tasks.load(task_id)
        if prepared is not None and (
            prepared.thread_id != goal.thread_id or prepared.status != "interrupted"
            or prepared.task_path != link.task_path or prepared.goal != goal.objective
        ):
            raise ValueError("目标的迁移位置已被占用；未覆盖现有任务。")
        store.tasks.bind({
            "thread_id": goal.thread_id, "task_id": task_id, "goal": goal.objective,
            "task_path": link.task_path, "status": "interrupted", "work_kind": "goal",
            "work_name": goal.name, "duration_seconds": goal.duration_seconds,
            "cancellation_scope": "detached" if goal.name else "foreground",
            "context_anchor_message_id": link.context_anchor_message_id,
        })
        migrated = store.goals.rebind_task(
            goal.thread_id, goal_id=goal.goal_id, expected_task_id=goal.task_id, task_id=task_id,
        )
        if migrated is None:
            raise ValueError("目标刚刚被其他操作修改，已保留现场，请重新查看 Goal。")
        return migrated
