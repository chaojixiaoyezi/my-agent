
from __future__ import annotations

"""LLM: 新建子代理的会话投影、父级等待和自动启动必须在一个生命周期出口汇合。

模块用途: 子代理记录创建后统一登记到当前会话、进度和后台执行链，并修正并发终态投影。
"""

from dataclasses import dataclass

from ...common.cancellation import raise_if_cancelled
from ...runtime_errors import runtime_error_report
from ...subagents.models import TaskStatus, normalize_task_status
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


# LLM: Materialization, recursive parent wait registration, conversation binding,
# and host start receipt form one lifecycle edge; no model-side dispatch step exists.
# 函数用途: 子代理记录落盘后统一绑定会话、登记父级等待并自动启动。
def publish_created_subagents(
    request: CreatedSubagentLifecycleRequest,
) -> CreatedSubagentLifecycleResult:
    """Register newly materialized subagents and optionally launch their runners."""
    tasks = [task for task in request.tasks if _task_id(task)]
    run_ids = [_task_id(task) for task in tasks]
    raise_if_cancelled()
    conversation_bind_errors = _bind_tasks_to_conversation(request.agent, tasks)
    raise_if_cancelled()
    remember_orchestration_run_ids(request.agent, run_ids)
    _mark_recursive_parent_wait(request, run_ids)
    raise_if_cancelled()
    auto_start = auto_start_tasks(request.agent, tasks, request.request_params)
    return CreatedSubagentLifecycleResult(
        auto_start=auto_start,
        run_ids=run_ids,
        conversation_bind_errors=conversation_bind_errors,
    )


# LLM: Only a real nested create owns a direct-parent wait marker. Top-level
# parents are conversation turns, while deferred/dry-run records never start.
# 函数用途: 在子代理真正创建下一层时记住它正在等哪些直属孩子。
def _mark_recursive_parent_wait(
    request: CreatedSubagentLifecycleRequest,
    run_ids: list[str],
) -> None:
    if not run_ids or bool(request.request_params.get("defer_start")):
        return
    from ...runtime_context import current_subagent_run_id
    from ...subagents.direct_parent_lifecycle import (
        mark_parent_waiting_for_direct_children,
    )

    parent_run_id = current_subagent_run_id(request.agent)
    if parent_run_id:
        mark_parent_waiting_for_direct_children(
            request.agent.subagents,
            parent_run_id,
            run_ids,
        )


# LLM: A child link is an addressable projection, never lifecycle authority. Binding must use
# canonical task identity and reconcile a terminal state that raced ahead of this projection.
# 函数用途: 把新子代理挂到父会话里供 TUI 查看，并避免已停止任务被迟到绑定显示成运行中。
def _bind_tasks_to_conversation(agent: object, tasks: list[object]) -> list[dict[str, object]]:
    """Make local subagent run_ids addressable in the parent conversation thread."""
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(getattr(store, 'tasks', None), 'bind', None)):
        return []
    bind_errors: list[dict[str, object]] = []
    for task in tasks:
        raise_if_cancelled()
        attrs = getattr(task, "attributes", {}) or {}
        if not isinstance(attrs, dict):
            continue
        thread_id = str(attrs.get("conversation_thread_id") or "").strip()
        if not thread_id:
            continue
        try:
            store.tasks.bind(
                {
                    "thread_id": thread_id,
                    "task_id": _task_id(task),
                    "goal": str(getattr(task, "goal", "") or ""),
                    "task_path": str(getattr(task, "task_dir", "") or ""),
                    "status": _conversation_status_for_task(task) or "active",
                }
            )
            reconcile_error = _reconcile_bound_task_status(agent, store, task)
            if reconcile_error is not None:
                bind_errors.append(reconcile_error)
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


# LLM: Conversation links are projections of canonical SubAgentTask state. A cancellation may
# land after task materialization but before this batch reaches bind_task, so reload after binding
# and retire the projection when needed; bind_task must never become a resurrection seam.
# 函数用途: 子代理会话行绑定后复读权威状态，修正“已取消子代理却仍显示运行中”的竞态。
def _reconcile_bound_task_status(
    agent: object,
    store: object,
    task: object,
) -> dict[str, object] | None:
    run_id = _task_id(task)
    manager = getattr(agent, "subagents", None)
    loader = getattr(manager, "load", None)
    updater = getattr(getattr(store, 'tasks', None), 'update_status', None)
    if not run_id or not callable(loader) or not callable(updater):
        return None
    try:
        canonical = loader(run_id)
        status = _conversation_status_for_task(canonical)
        if status:
            updater({"task_id": run_id, "status": status})
    except Exception as exc:
        return {
            "run_id": run_id,
            "thread_id": str(
                (getattr(task, "attributes", {}) or {}).get("conversation_thread_id")
                if isinstance(getattr(task, "attributes", {}), dict)
                else ""
            ),
            **runtime_error_report(
                exc,
                context="create_subagents.conversation.reconcile_status",
            ),
        }
    return None


# LLM: Only canonical terminal TaskStatus values may close a child conversation projection.
# BLOCKED stays active/addressable for parent guidance; unknown values fail open to the normal
# active bind and are surfaced by the canonical subagent state readers instead of guessed here.
# 函数用途: 把子代理权威终态转换为会话索引使用的非活跃状态。
def _conversation_status_for_task(task: object) -> str:
    try:
        status = normalize_task_status(getattr(task, "status", ""))
    except ValueError:
        return ""
    return {
        TaskStatus.DONE.value: "completed",
        TaskStatus.CANCELLED.value: "cancelled",
        TaskStatus.ABANDONED.value: "abandoned",
        TaskStatus.TAKEN_OVER.value: "taken_over",
        TaskStatus.FAILED.value: "failed",
        TaskStatus.TIMEOUT.value: "timeout",
        TaskStatus.CHANNEL_ERROR.value: "channel_error",
    }.get(status, "")


def _task_id(task: object) -> str:
    value = getattr(task, "id", "")
    return value.strip() if isinstance(value, str) else ""
