from __future__ import annotations

import time

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...subagents.models import TaskStatus

_REPLACEMENT_RECORD_SUCCESS = frozenset({"recorded", "already_recorded"})


# LLM: Replacement preflight trusts only explicit source run ids and canonical
# parent/takeover state. It never treats matching goals, covers, or outputs as authority.
# 函数用途: 在创建新 child 前确认每个被接管 run 存在、属于同一直属父级且尚未被别人接管。
def validate_create_replacements(agent: object, task_params: list[object]) -> list[dict[str, object]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return [{"reason": "subagent_manager_unavailable"}]
    issues: list[dict[str, object]] = []
    claimed: dict[str, int] = {}
    for index, params in enumerate(task_params):
        source_ids = _params_replacement_source_ids(params)
        parent_id = str(getattr(params, "parent_id", "") or "").strip()
        for source_id in source_ids:
            previous_index = claimed.get(source_id)
            if previous_index is not None:
                issues.append(
                    {
                        "index": index,
                        "source_run_id": source_id,
                        "reason": "duplicate_replacement_source",
                        "first_item_index": previous_index,
                    }
                )
                continue
            claimed[source_id] = index
            try:
                source = manager.load(source_id)
            except Exception as exc:
                issues.append(
                    {
                        "index": index,
                        "source_run_id": source_id,
                        "reason": "source_unavailable",
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            source_parent = str(getattr(source, "parent_id", "") or "").strip()
            if source_parent != parent_id:
                issues.append(
                    {
                        "index": index,
                        "source_run_id": source_id,
                        "reason": "source_not_direct_sibling",
                        "source_parent_id": source_parent,
                        "expected_parent_id": parent_id,
                    }
                )
            takeover_by = str(getattr(source, "takeover_by", "") or "").strip()
            if takeover_by:
                issues.append(
                    {
                        "index": index,
                        "source_run_id": source_id,
                        "reason": "source_already_taken_over",
                        "takeover_by": takeover_by,
                    }
                )
    return issues


# LLM: A replacement child may enter lifecycle publication only when every
# requested source edge was recorded or was already recorded to that exact child.
# 函数用途: 判断接管记录是否全部成功，供启动前的原子闸使用。
def replacement_records_allow_start(records: list[dict[str, object]]) -> bool:
    return all(str(record.get("status") or "") in _REPLACEMENT_RECORD_SUCCESS for record in records)


# LLM: A failed replacement edge must leave newly materialized children in a
# canonical terminal state before lifecycle publication; reused runs are never rewritten.
# 函数用途: 接管记录失败时取消本批尚未启动的新 child，避免留下永久占容量的 PLANNING 幻影。
def cancel_unstarted_replacement_tasks(
    manager: object,
    resolutions: list[object],
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    now = time.time()
    for resolution in resolutions:
        if bool(getattr(resolution, "reused", False)):
            continue
        task = getattr(resolution, "task", None)
        run_id = str(getattr(task, "id", "") or "").strip()
        if task is None or not run_id:
            continue
        try:
            task.status = TaskStatus.CANCELLED.value
            task.updated_at = now
            attrs = dict(getattr(task, "attributes", {}) or {})
            attrs["creation_abort"] = {
                "code": "SUBAGENT_REPLACEMENT_RECORD_FAILED",
                "at": now,
                "replacement_records": [dict(record) for record in records],
            }
            task.attributes = attrs
            manager.save(task)
            results.append({"run_id": run_id, "status": "cancelled_before_start"})
        except Exception as exc:
            results.append(
                {
                    "run_id": run_id,
                    "status": "cancel_failed",
                    "error_type": type(exc).__name__,
                }
            )
    return results


# LLM: CreateRunParams stores replacement ids inside its structured attributes;
# this reader accepts no aliases and does not inspect goal text.
# 函数用途: 从规范化创建参数里读取要接管的旧 run_id。
def _params_replacement_source_ids(params: object) -> list[str]:
    attrs = getattr(params, "attributes", None)
    values = attrs.get("replacement_for_run_ids") if isinstance(attrs, dict) else None
    return string_list(values, TOOL_TEXT_LIST_OPTIONS)


# LLM: Takeover edges are persisted from explicit structured ids only; callers must gate lifecycle publication on every returned status.
# 函数用途: 为新建 child 逐条写入显式接管关系，并返回每条边的真实落账结果。
def record_create_replacements(agent, tasks: list) -> list[dict[str, object]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return []
    records: list[dict[str, object]] = []
    for task in tasks:
        replacement_id = str(getattr(task, "id", "") or "").strip()
        if not replacement_id:
            continue
        for source_id in _replacement_source_ids(task):
            records.append(_record_single_replacement(manager, source_id, replacement_id))
    return records


# LLM: Source ids come only from the created task's normalized attributes and are de-duplicated without aliases.
# 函数用途: 读取一名 replacement child 明确声明接管的旧 run 编号。
def _replacement_source_ids(task: object) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    values = string_list(attrs.get("replacement_for_run_ids"), TOOL_TEXT_LIST_OPTIONS)
    task_id = str(getattr(task, "id", "") or "").strip()
    unique: list[str] = []
    for value in values:
        if value and value != task_id and value not in unique:
            unique.append(value)
    return unique


# LLM: One takeover write is monotonic and exposes missing/conflicting/storage failures as structured status rather than raising into startup.
# 函数用途: 精确记录一条旧 run 到新 run 的接管边，供创建闸决定是否允许新 child 启动。
def _record_single_replacement(manager, source_id: str, replacement_id: str) -> dict[str, object]:
    try:
        source = manager.load(source_id)
    except Exception as exc:
        return {
            "source_run_id": source_id,
            "replacement_run_id": replacement_id,
            "status": "source_missing",
            "error": f"{type(exc).__name__}: {exc}",
        }
    existing = str(getattr(source, "takeover_by", "") or "").strip()
    if existing == replacement_id:
        return {"source_run_id": source_id, "replacement_run_id": replacement_id, "status": "already_recorded"}
    if existing and existing != replacement_id:
        return {
            "source_run_id": source_id,
            "replacement_run_id": replacement_id,
            "status": "already_taken_over",
            "takeover_by": existing,
        }
    try:
        manager.record_takeover(
            source_id,
            take_over_by=replacement_id,
            reason="explicit replacement declared by create_subagents",
            locked_files=[],
        )
    except Exception as exc:
        return {
            "source_run_id": source_id,
            "replacement_run_id": replacement_id,
            "status": "record_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"source_run_id": source_id, "replacement_run_id": replacement_id, "status": "recorded"}


__all__ = [
    "cancel_unstarted_replacement_tasks",
    "record_create_replacements",
    "replacement_records_allow_start",
    "validate_create_replacements",
]
