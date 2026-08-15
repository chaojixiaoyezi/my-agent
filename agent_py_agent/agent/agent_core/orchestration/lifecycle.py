
from __future__ import annotations

from dataclasses import dataclass

from ...runtime_errors import runtime_error_report
from .background.dispatch import auto_start_tasks
from .run_scope import remember_orchestration_run_ids


@dataclass(frozen=True)
class CreatedSubagentLifecycleRequest:
    """Facts needed after a create/schedule path has materialized task records."""

    agent: object
    tasks: list[object]
    request_params: dict[str, object]


@dataclass(frozen=True)
class CreatedSubagentLifecycleResult:
    """Shared lifecycle output consumed by model-visible create/schedule payloads."""

    auto_start: dict[str, object]
    run_ids: list[str]
    conversation_bind_errors: list[dict[str, object]]


def publish_created_subagents(
    request: CreatedSubagentLifecycleRequest,
) -> CreatedSubagentLifecycleResult:
    """Register newly materialized subagents and optionally launch their runners."""
    tasks = [task for task in request.tasks if _task_id(task)]
    run_ids = [_task_id(task) for task in tasks]
    conversation_bind_errors = _bind_tasks_to_conversation(request.agent, tasks)
    remember_orchestration_run_ids(request.agent, run_ids)
    auto_start = auto_start_tasks(request.agent, tasks, request.request_params)
    return CreatedSubagentLifecycleResult(
        auto_start=auto_start,
        run_ids=run_ids,
        conversation_bind_errors=conversation_bind_errors,
    )


def _bind_tasks_to_conversation(agent: object, tasks: list[object]) -> list[dict[str, object]]:
    """Make local subagent run_ids addressable in the parent conversation thread."""
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "bind_task", None)):
        return []
    bind_errors: list[dict[str, object]] = []
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        if not isinstance(attrs, dict):
            continue
        thread_id = str(attrs.get("conversation_thread_id") or "").strip()
        if not thread_id:
            continue
        try:
            store.bind_task(
                {
                    "thread_id": thread_id,
                    "task_id": _task_id(task),
                    "goal": str(getattr(task, "goal", "") or ""),
                    "task_path": str(getattr(task, "task_dir", "") or ""),
                }
            )
        except Exception as exc:
            bind_errors.append(
                {
                    "run_id": _task_id(task),
                    "thread_id": thread_id,
                    **runtime_error_report(
                        exc,
                        context="create_subagents.conversation.bind_task",
                    ),
                }
            )
    return bind_errors


def _task_id(task: object) -> str:
    value = getattr(task, "id", "")
    return value.strip() if isinstance(value, str) else ""
