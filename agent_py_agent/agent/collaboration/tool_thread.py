
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..agent_core.runner.context import current_subagent_run_id
from ..runtime_errors import runtime_error_report
from ..tools import ToolExecutionResult
from .tool_values import error

if TYPE_CHECKING:
    from ..core import SimpleAgent

_LOGGER = logging.getLogger(__name__)


def resolve_thread(agent: SimpleAgent, params: dict[str, object], *, tool_name: str = "raise_collaboration") -> tuple[str, str] | ToolExecutionResult:
    thread_id = str(params.get("thread_id") or "").strip()
    task_id = str(params.get("task_id") or "").strip()
    if thread_id:
        return _explicit_thread(agent, thread_id, task_id, tool_name=tool_name)
    if task_id:
        resolved = thread_from_task(agent, task_id, materialize=True, tool_name=tool_name)
        if isinstance(resolved, ToolExecutionResult):
            return _current_runner_or_error(agent, resolved)
        if resolved:
            return resolved
    if resolved := thread_from_current_runner(agent):
        return resolved
    return error(tool_name, "thread_required", "thread_id is required unless task_id is bound to a thread")


def thread_from_task(
    agent: SimpleAgent,
    task_id: str,
    *,
    materialize: bool = False,
    tool_name: str = "raise_collaboration",
) -> tuple[str, str] | ToolExecutionResult | None:
    if not task_id:
        return None
    try:
        thread = agent.conversation_store.thread_for_task(task_id)
    except Exception as exc:
        return error(
            tool_name,
            "task_thread_lookup_failed",
            f"conversation task binding lookup failed: {task_id}",
            _load_error(exc, "raise_collaboration.thread_for_task"),
        )
    if thread is not None:
        return thread.thread_id, task_id
    linked = _thread_from_subagent_task(agent, task_id, tool_name=tool_name)
    if isinstance(linked, ToolExecutionResult):
        return linked
    if linked:
        return linked, task_id
    return _materialize_internal_thread_for_task(agent, task_id, tool_name=tool_name) if materialize else None


def thread_from_current_runner(agent: SimpleAgent) -> tuple[str, str] | ToolExecutionResult | None:
    run_id = current_subagent_run_id(agent)
    return thread_from_task(agent, run_id, materialize=True, tool_name="raise_collaboration") if run_id else None


def _explicit_thread(agent: SimpleAgent, thread_id: str, task_id: str, *, tool_name: str) -> tuple[str, str] | ToolExecutionResult:
    thread = _load_conversation_thread(
        agent,
        thread_id,
        context="raise_collaboration.load_thread",
        tool_name=tool_name,
    )
    if isinstance(thread, ToolExecutionResult):
        return thread
    if thread is not None:
        return thread_id, task_id
    if fallback := _fallback_thread(agent, task_id, tool_name):
        return fallback
    return error(tool_name, "unknown_thread", f"unknown conversation thread: {thread_id}")


def _fallback_thread(
    agent: SimpleAgent,
    task_id: str,
    tool_name: str,
) -> tuple[str, str] | ToolExecutionResult | None:
    task_fallback = _task_fallback_thread(agent, task_id, tool_name)
    if task_fallback and not isinstance(task_fallback, ToolExecutionResult):
        return task_fallback
    if current := thread_from_current_runner(agent):
        return current
    return task_fallback if isinstance(task_fallback, ToolExecutionResult) else None


def _task_fallback_thread(
    agent: SimpleAgent,
    task_id: str,
    tool_name: str,
) -> tuple[str, str] | ToolExecutionResult | None:
    if not task_id:
        return None
    return thread_from_task(agent, task_id, materialize=True, tool_name=tool_name)


def _materialize_internal_thread_for_task(
    agent: SimpleAgent,
    task_id: str,
    *,
    tool_name: str,
) -> tuple[str, str] | ToolExecutionResult | None:
    task = _load_subagent_task(agent, task_id, tool_name=tool_name)
    if isinstance(task, ToolExecutionResult):
        return task
    current = getattr(agent, "_current_run_params", None)
    if task is None and str(getattr(current, "task_id", "") or "").strip() != task_id:
        return None
    goal = str(getattr(task, "goal", "") or getattr(current, "prompt", "") or task_id)
    owner = str(getattr(task, "owner", "") or getattr(task, "agent_name", "") or "local-agent")
    try:
        thread = agent.conversation_store.get_or_create_thread(
            {
                "canonical_user_id": "local-agent",
                "channel": "internal",
                "channel_conversation_id": f"task:{task_id}",
                "channel_user_id": owner,
                "title": goal[:80],
            }
        )
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": goal,
                "status": str(getattr(task, "status", "") or "active"),
            }
        )
    except Exception as exc:
        return error(
            tool_name,
            "internal_thread_materialize_failed",
            f"internal collaboration thread materialization failed: {task_id}",
            _load_error(exc, "raise_collaboration.materialize_internal_thread"),
        )
    _remember_thread_on_task(agent, task, thread.thread_id, task_id)
    return thread.thread_id, task_id


def _thread_from_subagent_task(agent: SimpleAgent, task_id: str, *, tool_name: str) -> str | ToolExecutionResult:
    task = _load_subagent_task(agent, task_id, tool_name=tool_name)
    if isinstance(task, ToolExecutionResult):
        return task
    attrs = getattr(task, "attributes", {}) if task is not None else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    if not thread_id:
        return ""
    thread = _load_conversation_thread(
        agent,
        thread_id,
        context="raise_collaboration.load_linked_thread",
        tool_name=tool_name,
    )
    if isinstance(thread, ToolExecutionResult):
        return thread
    return thread_id if thread is not None else ""


def _load_conversation_thread(
    agent: SimpleAgent,
    thread_id: str,
    *,
    context: str,
    tool_name: str,
):
    try:
        thread, load_error = _load_thread_with_report(agent, thread_id)
    except Exception as exc:
        return error(
            tool_name,
            "thread_lookup_failed",
            f"conversation thread lookup failed: {thread_id}",
            _load_error(exc, context),
        )
    if load_error is not None:
        return error(
            tool_name,
            "thread_lookup_failed",
            f"conversation thread lookup failed: {thread_id}",
            _report_load_error(load_error, context),
        )
    return thread


def _load_thread_with_report(agent: SimpleAgent, thread_id: str):
    if callable(getattr(agent.conversation_store, "load_thread_report", None)):
        return agent.conversation_store.load_thread_report(thread_id)
    return agent.conversation_store.load_thread(thread_id), None


def _load_subagent_task(agent: SimpleAgent, task_id: str, *, tool_name: str):
    try:
        return agent.subagents.load(task_id)
    except Exception as exc:
        return error(
            tool_name,
            "subagent_task_load_failed",
            f"subagent task lookup failed while resolving collaboration thread: {task_id}",
            _load_error(exc, "raise_collaboration.subagents.load"),
        )


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
    except Exception as exc:
        report = runtime_error_report(exc, context="raise_collaboration.remember_thread_on_task")
        report["task_id"] = task_id
        report["thread_id"] = thread_id
        _LOGGER.warning("collaboration thread binding could not be saved on task: %s", report)


def _load_error(exc: BaseException, context: str) -> dict[str, object]:
    return {"load_error": runtime_error_report(exc, context=context)}


def _report_load_error(report: dict[str, object], context: str) -> dict[str, object]:
    payload = dict(report)
    payload["read_context"] = payload.get("context", "")
    payload["context"] = context
    return {"load_error": payload}


def _current_runner_or_error(agent: SimpleAgent, fallback_error: ToolExecutionResult) -> tuple[str, str] | ToolExecutionResult:
    current = thread_from_current_runner(agent)
    return current if current else fallback_error
