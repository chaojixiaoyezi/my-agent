from __future__ import annotations

"""Structured task state layered onto one thread history.

Task progress and workspace identity are operational facts.  They never form a
second model history and never carry a separate compact package.
"""

from pathlib import Path
from typing import Any

from ..agent_core.orchestration.dispatch_progress_seed import reconcile_completed_child_items
from ..agent_core.runtime.owner_roots import runtime_owner_root
from ..agent_core.runtime.task_identity import task_path_progress_ledger_id
from ..runtime_errors import runtime_error_report
from ..task_progress import (
    read_task_progress_report,
    task_progress_status_is_closed,
    task_progress_summary,
)

_PLAN_CONTINUATION_ID_LIMIT = 64


# LLM: Background turns must read the same task-path progress ledger that main
# tool calls write. The durable task id remains lineage identity only.
# 函数用途: 汇总当前任务的目录、状态和真实进度清单，供后台主代理续跑时读取。
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
    link = _task_link(store, thread_id, selected_id, load_errors)
    task_root = _task_root(link)
    owner_root = runtime_owner_root(agent)
    ledger_id = (
        task_path_progress_ledger_id(getattr(link, "task_path", "")) or selected_id
    )
    reconcile_completed_child_items(
        agent,
        owner_root,
        ledger_id,
        task_root=task_root,
    )
    progress, load_error = read_task_progress_report(owner_root, ledger_id)
    if load_error is not None:
        load_errors.append(load_error)
    work_kind = str(getattr(link, "work_kind", "") or "")
    progress_projection = task_progress_summary(progress)
    state: dict[str, Any] = {
        "schema_version": "task-runtime-state.v1",
        "task_id": selected_id,
        "goal": str(getattr(link, "goal", "") or ""),
        "status": str(getattr(link, "status", "") or ""),
        "created_at": float(getattr(link, "created_at", 0.0) or 0.0),
        "task_path": str(task_root or ""),
        "work_kind": work_kind,
        "work_name": str(getattr(link, "work_name", "") or ""),
        "duration_seconds": getattr(link, "duration_seconds", None),
        "expires_at": getattr(link, "expires_at", None),
        "cancellation_scope": str(
            getattr(link, "cancellation_scope", "") or "foreground"
        ),
        "task_progress": progress_projection,
    }
    if continuation := _plan_continuation_contract(progress, progress_projection):
        state["plan_continuation"] = continuation
    if work_kind.strip().lower() == "audit":
        from ..ingestion.audit_state import (
            audit_task_source_facts,
            audit_task_summary_facts,
        )

        state["audit_sources"] = audit_task_source_facts(agent, selected_id)
        state["audit_summary"] = audit_task_summary_facts(agent, selected_id)
    return state


# LLM: A lifecycle wake must continue the canonical plan by exact item id. This projection is
# typed model context only; it neither infers title similarity nor mutates progress or child state.
# 函数用途: 向后台续跑明确现有 Todo 的唯一编号和子代理 covers 绑定字段，避免重复建同义清单。
def _plan_continuation_contract(
    progress: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    items = [item for item in progress.get("items", []) if isinstance(item, dict)]
    all_ids = list(
        dict.fromkeys(
            str(item.get("id") or "").strip()
            for item in items
            if str(item.get("id") or "").strip()
        )
    )
    if not all_ids:
        return {}
    open_ids = [
        str(item.get("id") or "").strip()
        for item in items
        if str(item.get("id") or "").strip()
        and not task_progress_status_is_closed(item.get("status"))
    ]
    limit = _PLAN_CONTINUATION_ID_LIMIT
    return {
        "schema_version": "plan-continuation.v1",
        "ledger_run_id": str(summary.get("run_id") or ""),
        "ledger_ref": str(summary.get("ref") or ""),
        "identity_field": "task_progress.items[].id",
        "existing_item_ids": all_ids[:limit],
        "open_item_ids": open_ids[:limit],
        "existing_item_count": len(all_ids),
        "ids_truncated": len(all_ids) > limit or len(open_ids) > limit,
        "reuse_policy": "reuse_existing_ids",
        "full_ledger_read": {
            "tool": "task_progress",
            "action": "read",
            "run_id": str(summary.get("run_id") or ""),
        },
        "subagent_binding": {
            "tool": "create_subagents",
            "field": "items[].covers",
            "value_source": "open_item_ids",
            "matching": "exact_id_only",
        },
    }


# LLM: Task-link corruption is surfaced through load_errors while other valid
# links remain usable; do not silently manufacture a link from task prose.
# 函数用途: 在当前会话中按精确任务编号查找任务链接，并记录读取错误。
def _task_link(
    store: object,
    thread_id: str,
    task_id: str,
    load_errors: list[dict[str, Any]],
) -> object | None:
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
        return link
    return None


# LLM: A nonexistent task directory cannot be used for canonical child-state
# reconciliation, though its raw linked path may still identify the ledger.
# 函数用途: 把任务链接里的路径解析成当前确实存在的任务目录。
def _task_root(link: object | None) -> Path | None:
    value = str(getattr(link, "task_path", "") or "").strip()
    if not value:
        return None
    root = Path(value).expanduser().resolve(strict=False)
    return root if root.exists() else None


__all__ = ["task_runtime_state"]
