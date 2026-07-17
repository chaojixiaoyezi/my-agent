from __future__ import annotations

"""Structured task state layered onto one thread history.

Task progress and workspace identity are operational facts.  They never form a
second model history and never carry a separate compact package.
"""

from pathlib import Path
from typing import Any

from ..agent_core.orchestration.dispatch_progress_seed import reconcile_completed_child_items
from ..agent_core.runtime.owner_roots import runtime_owner_root
from ..runtime_errors import runtime_error_report
from ..task_progress import read_task_progress_report, task_progress_summary


def task_runtime_state(
    *,
    agent: object,
    store: object,
    thread_id: str,
    task_id: str,
    load_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return progress and workspace identity for one task in the current thread."""
    selected_id = str(task_id or "").strip()
    if not selected_id:
        return {}
    task_root = _task_root(store, thread_id, selected_id, load_errors)
    owner_root = runtime_owner_root(agent)
    reconcile_completed_child_items(
        agent,
        owner_root,
        selected_id,
        task_root=task_root,
    )
    progress, load_error = read_task_progress_report(owner_root, selected_id)
    if load_error is not None:
        load_errors.append(load_error)
    return {
        "schema_version": "task-runtime-state.v1",
        "task_id": selected_id,
        "task_path": str(task_root or ""),
        "task_progress": task_progress_summary(progress),
    }


def _task_root(
    store: object,
    thread_id: str,
    task_id: str,
    load_errors: list[dict[str, Any]],
) -> Path | None:
    try:
        if callable(getattr(store, "task_links_report", None)):
            links, errors = store.task_links_report(thread_id)
            load_errors.extend(item for item in errors if isinstance(item, dict))
        else:
            links = store.task_links(thread_id)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="task_runtime_state.task_link"))
        return None
    for link in links:
        if str(getattr(link, "task_id", "") or "").strip() != task_id:
            continue
        value = str(getattr(link, "task_path", "") or "").strip()
        if not value:
            return None
        root = Path(value).expanduser().resolve(strict=False)
        return root if root.exists() else None
    return None


__all__ = ["task_runtime_state"]
