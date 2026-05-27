# LLM: Collaboration thread resolution keeps cases attached to durable conversations.
# 模块用途: 从 thread_id、task_id 或当前 runner 上下文解析/物化长期会话 thread。

from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent_core.runner_context import current_subagent_run_id
from ..tools import ToolExecutionResult
from .tool_values import error

if TYPE_CHECKING:
    from ..core import SimpleAgent


def resolve_thread(agent: SimpleAgent, params: dict[str, object]) -> tuple[str, str] | ToolExecutionResult:
    thread_id = str(params.get("thread_id") or "").strip()
    task_id = str(params.get("task_id") or "").strip()
    if thread_id:
        return _explicit_thread(agent, thread_id, task_id)
    if task_id and (resolved := thread_from_task(agent, task_id, materialize=True)):
        return resolved
    if resolved := thread_from_current_runner(agent):
        return resolved
    return error("open_case", "thread_required", "thread_id is required unless task_id is bound to a thread")


def thread_from_task(agent: SimpleAgent, task_id: str, *, materialize: bool = False) -> tuple[str, str] | None:
    if not task_id:
        return None
    thread = agent.conversation_store.thread_for_task(task_id)
    if thread is not None:
        return thread.thread_id, task_id
    if linked := _thread_from_subagent_task(agent, task_id):
        return linked, task_id
    return _materialize_internal_thread_for_task(agent, task_id) if materialize else None


def thread_from_current_runner(agent: SimpleAgent) -> tuple[str, str] | None:
    run_id = current_subagent_run_id(agent)
    return thread_from_task(agent, run_id, materialize=True) if run_id else None


def _explicit_thread(agent: SimpleAgent, thread_id: str, task_id: str) -> tuple[str, str] | ToolExecutionResult:
    if agent.conversation_store.load_thread(thread_id) is not None:
        return thread_id, task_id
    for fallback in (thread_from_task(agent, task_id, materialize=True), thread_from_current_runner(agent)):
        if fallback:
            return fallback
    return error("open_case", "unknown_thread", f"unknown conversation thread: {thread_id}")


def _materialize_internal_thread_for_task(agent: SimpleAgent, task_id: str) -> tuple[str, str] | None:
    task = _load_subagent_task(agent, task_id)
    current = getattr(agent, "_current_run_params", None)
    if task is None and str(getattr(current, "task_id", "") or "").strip() != task_id:
        return None
    goal = str(getattr(task, "goal", "") or getattr(current, "prompt", "") or task_id)
    owner = str(getattr(task, "owner", "") or getattr(task, "agent_name", "") or "local-agent")
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "local-agent", 'channel': "internal", 'channel_conversation_id': f"task:{task_id}", 'channel_user_id': owner, 'title': goal[:80]})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': task_id, 'goal': goal, 'status': str(getattr(task, "status", "") or "active")})
    _remember_thread_on_task(agent, task, thread.thread_id, task_id)
    return thread.thread_id, task_id


def _thread_from_subagent_task(agent: SimpleAgent, task_id: str) -> str:
    task = _load_subagent_task(agent, task_id)
    attrs = getattr(task, "attributes", {}) if task is not None else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    return thread_id if thread_id and agent.conversation_store.load_thread(thread_id) is not None else ""


def _load_subagent_task(agent: SimpleAgent, task_id: str):
    try:
        return agent.subagents.load(task_id)
    except Exception:
        return None


def _remember_thread_on_task(agent: SimpleAgent, task, thread_id: str, task_id: str) -> None:
    attrs = getattr(task, "attributes", {}) if task is not None else {}
    if not isinstance(attrs, dict):
        return
    updated = {**attrs, "conversation_thread_id": thread_id, "conversation_task_id": task_id}
    if updated == attrs:
        return
    task.attributes = updated
    try:
        agent.subagents.save(task)
    except Exception:
        pass
