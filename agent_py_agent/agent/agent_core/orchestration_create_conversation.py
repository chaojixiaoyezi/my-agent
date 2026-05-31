# LLM: Create-subagent conversation binding lets local and provider runs wake their parent thread.
# 模块用途: 将当前主任务 thread/task 绑定写入子代理 attributes；不改变模型执行流程。

from __future__ import annotations


# LLM: add_current_conversation_attrs propagates durable thread binding to spawned agents.
# 函数用途: create_subagents 在长期会话 run 内调用时，把 thread/task 绑定写入 task.attributes；
# 后续子/孙代理可用自己的 run_id 反查会话，不要求模型手填 thread_id。
def add_current_conversation_attrs(attrs: dict[str, object], agent) -> None:
    if agent is None:
        return
    current = getattr(agent, "_current_run_params", None)
    raw_task_id = getattr(current, "task_id", "") if current is not None else ""
    if not isinstance(raw_task_id, str):
        return
    task_id = raw_task_id.strip()
    if not task_id:
        return
    try:
        thread = agent.conversation_store.thread_for_task(task_id)
    except Exception:
        thread = None
    if thread is None:
        thread = _materialize_internal_thread(agent, current, task_id)
    _attach_thread_attrs(attrs, thread, task_id)


def _materialize_internal_thread(agent, current, task_id: str):
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None
    goal = str(getattr(current, "root_user_prompt", "") or getattr(current, "prompt", "") or task_id).strip()
    try:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": "local-agent",
                "channel": "internal",
                "channel_conversation_id": f"task:{task_id}",
                "channel_user_id": "local-main-agent",
                "title": goal[:80] or task_id,
            }
        )
        store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": goal or task_id,
                "status": "active",
            }
        )
        return thread
    except Exception:
        return None


def _attach_thread_attrs(attrs: dict[str, object], thread: object, task_id: str) -> None:
    thread_id = getattr(thread, "thread_id", "") if thread is not None else ""
    if not isinstance(thread_id, str) or not thread_id.strip():
        return
    attrs.setdefault("conversation_thread_id", thread_id.strip())
    attrs.setdefault("conversation_task_id", task_id)


__all__ = ["add_current_conversation_attrs"]
