from __future__ import annotations

"""Compact, task-scoped facts for durable foreground/background continuation."""

from pathlib import Path
from typing import Any

from ..agent_core.orchestration.dispatch_progress_seed import reconcile_completed_child_items
from ..agent_core.runtime.owner_roots import runtime_owner_root
from ..runtime_errors import runtime_error_report
from ..task_progress import read_task_progress_report, task_progress_summary
from ..user_space.home_runtime_compact_refs import task_compact_payload
from ..user_space.task_compact_rollup import sync_task_compact_rollup


def task_continuation_context(
    *,
    agent: object,
    store: object,
    thread_id: str,
    task_id: str,
    load_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the existing task ledger and compact refs for one exact task id."""
    selected_id = str(task_id or "").strip()
    if not selected_id:
        return {}
    task_root = _task_root(store, thread_id, selected_id, load_errors)
    owner_root = runtime_owner_root(agent)
    completed_items = reconcile_completed_child_items(
        agent,
        owner_root,
        selected_id,
        task_root=task_root,
    )
    if completed_items and task_root is not None:
        try:
            sync_task_compact_rollup(task_root)
        except Exception as exc:
            load_errors.append(runtime_error_report(exc, context="task_continuation.compact_sync"))
    progress, load_error = read_task_progress_report(owner_root, selected_id)
    if load_error is not None:
        load_errors.append(load_error)
    compact = task_compact_payload(task_root / "work" / "compact") if task_root is not None else {}
    compact_errors = compact.get("load_errors") if isinstance(compact, dict) else None
    if isinstance(compact_errors, list):
        load_errors.extend(item for item in compact_errors if isinstance(item, dict))
    return {
        "schema_version": "task-continuation-context.v1",
        "task_id": selected_id,
        "task_path": str(task_root or ""),
        "task_progress": task_progress_summary(progress),
        "compact": compact,
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
        load_errors.append(runtime_error_report(exc, context="task_continuation.task_link"))
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


__all__ = ["task_continuation_context"]
