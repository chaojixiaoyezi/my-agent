
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...common.value_parsing import dedupe_strings
from ...runtime_errors import runtime_error_report


def source_work_state(restore_refs: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    load_errors: list[dict[str, object]] = []
    for ref in restore_refs["source_refs"]["snapshot_files"]:
        payload, error = _read_json_dict_report(Path(ref["path"]), context="compact_work_state.snapshot")
        if error:
            load_errors.append(error)
        candidate = _snapshot_work_state_candidate(payload)
        if candidate:
            candidate["source_load_errors"] = load_errors
            return candidate
    archive_state = _archive_work_state(restore_refs, scope)
    load_errors.extend(_source_load_errors(archive_state))
    if archive_state["goal"] or archive_state["next_actions"]:
        archive_state["source_load_errors"] = load_errors
        return archive_state
    return {"goal": "", "next_actions": [], "content_paths": [], "task_refs": [], "source_load_errors": load_errors}


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
    records, load_errors = _archive_records_report(restore_refs, scope)
    return {
        "goal": _archive_goal(records),
        "next_actions": _archive_next_actions(records),
        "content_paths": _archive_texts(records, "content_paths"),
        "task_refs": _archive_task_refs(records),
        "source_load_errors": load_errors,
    }


def _archive_records(restore_refs: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    records, _load_errors = _archive_records_report(restore_refs, scope)
    return records


def _archive_records_report(restore_refs: dict[str, Any], scope: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs.get("source_refs"), dict) else {}
    archive_refs = refs.get("archive_files", []) if isinstance(refs.get("archive_files"), list) else []
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    for ref in archive_refs:
        report_records, report_errors = _read_jsonl_dicts_report(
            Path(str(ref.get("path", ""))),
            context="compact_work_state.archive",
        )
        records.extend(record for record in report_records if _record_matches_scope(record, scope))
        load_errors.extend(report_errors)
    return records, load_errors


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
    return dedupe_strings([item for record in records for item in _text_list(record.get(key))])


def _archive_task_refs(records: list[dict[str, Any]]) -> list[str]:
    values = [item for record in records for item in _text_list(record.get("task_refs"))]
    for record in records:
        values.extend(str(record.get(key) or "").strip() for key in ("task_id", "run_id") if record.get(key))
    return dedupe_strings(values)


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    records, _load_errors = _read_jsonl_dicts_report(path, context="compact_work_state.archive")
    return records


def _read_jsonl_dicts_report(path: Path, *, context: str) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    if not path.exists():
        return [], [_load_error(path, FileNotFoundError(str(path)), context=context)]
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [], [_load_error(path, exc, context=context)]
    for line_no, line in enumerate(lines, start=1):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            load_errors.append(_load_error(path, exc, context=context, line_no=line_no))
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records, load_errors


def _read_json_dict(path: Path) -> dict[str, Any]:
    payload, _load_error = _read_json_dict_report(path, context="compact_work_state.snapshot")
    return payload


def _read_json_dict_report(path: Path, *, context: str) -> tuple[dict[str, Any], dict[str, object] | None]:
    if not path.exists():
        return {}, _load_error(path, FileNotFoundError(str(path)), context=context)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {}, _load_error(path, exc, context=context)
    if isinstance(payload, dict):
        return payload, None
    return {}, _load_error(path, ValueError(f"JSON root is {type(payload).__name__}, expected object"), context=context)


def _load_error(path: Path, exc: BaseException, *, context: str, line_no: int = 0) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    if line_no:
        report["line_no"] = line_no
    return report


def _source_load_errors(value: dict[str, Any]) -> list[dict[str, object]]:
    items = value.get("source_load_errors")
    return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _first_text(value: Any) -> str:
    items = _text_list(value)
    return items[0] if items else ""


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


__all__ = ["source_work_state"]
