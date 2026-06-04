
from __future__ import annotations

from pathlib import Path
from typing import Any


def collect_target_coverage_records(payloads: list[object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for payload in payloads:
        _collect_records(payload, records)
    return _unique_records(records)


def target_coverage_status(
    contract: dict[str, Any],
    *,
    coverage_records: list[dict[str, object]],
) -> dict[str, object]:
    targets = _target_items(contract)
    covered_keys = {
        key
        for record in coverage_records
        if _record_counts_as_covered(record)
        for key in _coverage_keys(record)
    }
    missing = [item for item in targets if not (_target_keys(item) & covered_keys)]
    enforcement = str(contract.get("enforcement") or "advisory").strip().lower()
    should_block = bool(missing) and enforcement in {"required", "strict", "hard", "block", "blocking", "enforced"}
    return {
        "scope_label": str(contract.get("scope_label") or ""),
        "enforcement": enforcement or "advisory",
        "expected_count": len(targets),
        "covered_count": len(targets) - len(missing),
        "missing_count": len(missing),
        "missing_items": missing[:50],
        "coverage_records": coverage_records[:100],
        "should_block": should_block,
        "recommended_next_action": "cover_missing_targets_before_submit" if should_block else "continue_or_summarize_with_missing_items_visible",
    }


def _collect_records(value: object, records: list[dict[str, object]]) -> None:
    if isinstance(value, dict):
        _collect_from_mapping(value, records)
    for child in _children(value):
        _collect_records(child, records)


def _children(value: object) -> list[object]:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _collect_from_mapping(value: dict[str, object], records: list[dict[str, object]]) -> None:
    records.extend(_direct_coverage_records(value.get("coverage_records")))
    records.extend(_registry_coverage_records(value.get("artifact_registry_refs")))
    if record := _tool_call_coverage_record(value):
        records.append(record)


def _direct_coverage_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        dict(row)
        for row in value
        if isinstance(row, dict) and str(row.get("target_id") or "").strip()
    ]


def _registry_coverage_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for row in value:
        if isinstance(row, dict):
            records.extend(_records_from_registry_row(row))
    return records


def _records_from_registry_row(row: dict[str, object]) -> list[dict[str, object]]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return []
    coverage = metadata.get("coverage_items")
    if not isinstance(coverage, list | tuple | set):
        return []
    artifact_ref = str(row.get("path") or row.get("artifact_id") or "")
    return [
        {
            "target_id": str(item).strip(),
            "status": "covered",
            "artifact_ref": artifact_ref,
        }
        for item in coverage
        if str(item).strip()
    ]


def _target_items(contract: dict[str, Any]) -> list[dict[str, str]]:
    raw = _first_list(contract, ("target_items", "items", "targets"))
    if not isinstance(raw, list):
        return []
    return [row for item in raw if (row := _target_item_row(item))]


def _first_list(payload: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return None


def _target_item_row(item: object) -> dict[str, str]:
    if isinstance(item, dict):
        target_id = str(item.get("target_id") or item.get("id") or item.get("label") or "").strip()
        row = {"target_id": target_id, "label": str(item.get("label") or target_id)} if target_id else {}
        for key in ("path", "source_path", "artifact_ref", "source_ref"):
            text = str(item.get(key) or "").strip()
            if text:
                row[key] = text
        return row
    target_id = str(item or "").strip()
    return {"target_id": target_id, "label": target_id} if target_id else {}


def _record_counts_as_covered(record: dict[str, object]) -> bool:
    status = str(record.get("status") or "covered").strip().lower()
    return bool(str(record.get("target_id") or "").strip()) and status in {"covered", "done", "ok", "ready", "hit"}


def _tool_call_coverage_record(value: dict[str, object]) -> dict[str, object]:
    tool = str(value.get("tool") or value.get("tool_name") or "").strip()
    if tool not in {"read_file", "read_artifact", "list_files", "find_files", "search_text"}:
        return {}
    if value.get("ok") is False:
        return {}
    parameters = value.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    source = str(
        value.get("source_path")
        or parameters.get("path")
        or parameters.get("file_path")
        or parameters.get("query")
        or ""
    ).strip()
    if not source:
        return {}
    return {
        "target_id": _canonical_ref(source),
        "status": "covered",
        "source_ref": source,
        "tool": tool,
    }


def _target_keys(item: dict[str, str]) -> set[str]:
    keys: set[str] = set()
    for key in ("target_id", "path", "source_path", "artifact_ref", "source_ref"):
        _add_ref_keys(keys, item.get(key))
    return keys


def _coverage_keys(record: dict[str, object]) -> set[str]:
    keys: set[str] = set()
    for key in ("target_id", "artifact_ref", "source_ref", "path"):
        _add_ref_keys(keys, record.get(key))
    return keys


def _add_ref_keys(keys: set[str], value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    keys.add(text)
    keys.add(_canonical_ref(text))


def _canonical_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return text


def _unique_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        target_id = str(record.get("target_id") or "").strip()
        artifact_ref = str(record.get("artifact_ref") or record.get("source_ref") or "").strip()
        key = (target_id, artifact_ref)
        if not target_id or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique
