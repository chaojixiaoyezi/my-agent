from __future__ import annotations

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list


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


__all__ = ["record_create_replacements"]
