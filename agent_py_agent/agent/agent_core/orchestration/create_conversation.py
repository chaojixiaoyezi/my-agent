
from __future__ import annotations

from ...runtime_errors import runtime_error_report


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
    lookup_error = None
    try:
        thread = agent.conversation_store.thread_for_task(task_id)
    except Exception as exc:
        lookup_error = exc
        thread = None
    materialize_error = None
    if thread is None:
        thread, materialize_error = _materialize_internal_thread(agent, current, task_id)
    if thread is None:
        _attach_conversation_errors(attrs, lookup_error, materialize_error)
    _attach_thread_attrs(attrs, thread, task_id)


def _materialize_internal_thread(agent, current, task_id: str) -> tuple[object | None, BaseException | None]:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None, None
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
        return thread, None
    except Exception as exc:
        return None, exc


def _attach_conversation_errors(
    attrs: dict[str, object],
    lookup_error: BaseException | None,
    materialize_error: BaseException | None,
) -> None:
    if lookup_error is not None:
        attrs.setdefault(
            "conversation_thread_lookup_error",
            runtime_error_report(lookup_error, context="conversation.thread_for_task"),
        )
    if materialize_error is not None:
        attrs.setdefault(
            "conversation_thread_materialize_error",
            runtime_error_report(materialize_error, context="conversation.materialize_internal_thread"),
        )


def _attach_thread_attrs(attrs: dict[str, object], thread: object, task_id: str) -> None:
    thread_id = getattr(thread, "thread_id", "") if thread is not None else ""
    if not isinstance(thread_id, str) or not thread_id.strip():
        return
    attrs.setdefault("conversation_thread_id", thread_id.strip())
    attrs.setdefault("conversation_task_id", task_id)


__all__ = ["add_current_conversation_attrs"]
