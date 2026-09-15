# LLM: All objective edits use exact Goal identity and content CAS in the owner store. No model calls or permission expansion.
# 模块用途: 统一主子代理的目标草稿保存，保留运行状态和用量，通知正在运行的代理读取新要求。
from __future__ import annotations

from .goal_prompting import objective_updated_prompt


# LLM: Goal revision CAS supplies idempotency. Definition updates use durable guidance, not a receipt pinned to an already active input turn.
# 函数用途: 比较草稿版本后保存正文并向本代理投递长期引导；暂停不恢复，重复旧版本保存不重复投递。
def save_goal_objective(agent, store, goal, *, objective: str, expected_revision: int, child: bool = False):
    text = str(objective or "").strip()
    if not text:
        raise ValueError("目标内容不能为空。")
    with store.goal_transition_guard(goal.thread_id):
        updated = store.update_goal({
            "thread_id": goal.thread_id, "goal_id": goal.goal_id, "objective": text,
            "expected_revision": expected_revision,
        })
        if updated is None:
            return None
        if not child:
            store.update_task_goal({"task_id": updated.task_id, "goal": updated.objective})
            agent.local_store.task_registry.update_task_description(updated.task_id, updated.objective)
        store.append_guidance({
            "target_type": "agent_run" if child else "task", "target_id": updated.task_id,
            "message": objective_updated_prompt(updated), "sender": "goal", "priority": "urgent",
            "metadata": {"goal_id": updated.goal_id, "revision": updated.revision, "kind": "goal_objective_updated"},
        })
    return updated
