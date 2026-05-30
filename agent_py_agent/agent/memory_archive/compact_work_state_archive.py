# LLM: Compact work-state archive fallback reads only restore refs and never scans broadly.
# 模块用途: 从 compact restore refs 的 snapshot/raw/hook 中恢复目标和下一步，供 work_state 使用。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def source_work_state(restore_refs: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    for ref in restore_refs["source_refs"]["snapshot_files"]:
        payload = _read_json_dict(Path(ref["path"]))
        candidate = _snapshot_work_state_candidate(payload)
        if candidate:
            return candidate
    archive_state = _archive_work_state(restore_refs, scope)
    if archive_state["goal"] or archive_state["next_actions"]:
        return archive_state
    return {"goal": "", "next_actions": [], "content_paths": [], "task_refs": []}


def _snapshot_work_state_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    candidate = {
        "goal": _first_text(payload.get("user_intents")),
        "next_actions": _text_list(payload.get("next_actions")),
        "content_paths": _text_list(payload.get("content_paths")),
        "task_refs": _text_list(payload.get("task_refs")),
    }
    return candidate if candidate["goal"] or candidate["content_paths"] or candidate["task_refs"] else {}


def _archive_work_state(restore_refs: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    records = _archive_records(restore_refs, scope)
    return {
        "goal": _archive_goal(records),
        "next_actions": _archive_next_actions(records),
        "content_paths": _archive_texts(records, "content_paths"),
        "task_refs": _archive_task_refs(records),
    }


def _archive_records(restore_refs: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs.get("source_refs"), dict) else {}
    archive_refs = refs.get("archive_files", []) if isinstance(refs.get("archive_files"), list) else []
    return [
        record
        for ref in archive_refs
        for record in _read_jsonl_dicts(Path(str(ref.get("path", ""))))
        if _record_matches_scope(record, scope)
    ]


def _record_matches_scope(record: dict[str, Any], scope: dict[str, Any]) -> bool:
    for key in ("session_id", "request_id", "run_id", "task_id"):
        expected = str(scope.get(key) or "")
        if expected and expected not in _record_scope_values(record, key):
            return False
    return True


def _record_scope_values(record: dict[str, Any], key: str) -> set[str]:
    values = {str(record.get(key) or "").strip()}
    turn_range = record.get("turn_range")
    if isinstance(turn_range, dict):
        values.add(str(turn_range.get(key) or "").strip())
    dispatch_events = record.get("dispatch_events")
    if isinstance(dispatch_events, list):
        values.update(str(item.get(key) or "").strip() for item in dispatch_events if isinstance(item, dict))
    return {value for value in values if value}


def _archive_goal(records: list[dict[str, Any]]) -> str:
    return _first_nonempty([_first_text(record.get("user_intents")) for record in records]) or _first_nonempty(
        [_raw_user_text(record) for record in records]
    )


def _raw_user_text(record: dict[str, Any]) -> str:
    if str(record.get("speaker") or "") != "user":
        return ""
    return str(record.get("content") or record.get("content_preview") or "").strip()


def _first_nonempty(values: list[str]) -> str:
    return next((value for value in values if value), "")


def _archive_next_actions(records: list[dict[str, Any]]) -> list[str]:
    for record in records:
        items = _action_first_items(_text_list(record.get("next_actions")))
        if items:
            return items
    for record in reversed(records):
        if str(record.get("action") or "") != "assistant_tool_round":
            continue
        hint = str(record.get("content") or record.get("content_preview") or "").strip()
        if hint and not _looks_like_raw_tool_step(hint) and not _looks_like_reader_first_recovery_hint(hint):
            return [hint]
    return []


def _looks_like_raw_tool_step(value: str) -> bool:
    text = value.strip()
    return text.startswith("[TOOL_CALL]") or "[/TOOL_CALL]" in text


def _action_first_items(items: list[str]) -> list[str]:
    return [item for item in items if not _looks_like_reader_first_recovery_hint(item)]


def _looks_like_reader_first_recovery_hint(value: str) -> bool:
    text = value.strip().lower()
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    reader_markers = ("先查看", "先读取", "read ", "inspect ", "查看", "读取")
    return any(marker in text for marker in recovery_markers) and any(marker in text for marker in reader_markers)


def _archive_texts(records: list[dict[str, Any]], key: str) -> list[str]:
    return _dedupe([item for record in records for item in _text_list(record.get(key))])


def _archive_task_refs(records: list[dict[str, Any]]) -> list[str]:
    values = [item for record in records for item in _text_list(record.get("task_refs"))]
    for record in records:
        values.extend(str(record.get(key) or "").strip() for key in ("task_id", "run_id") if record.get(key))
    return _dedupe(values)


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_text(value: Any) -> str:
    items = _text_list(value)
    return items[0] if items else ""


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


__all__ = ["source_work_state"]
