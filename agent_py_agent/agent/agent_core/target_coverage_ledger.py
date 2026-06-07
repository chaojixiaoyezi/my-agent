from __future__ import annotations

from pathlib import Path
from typing import Any


def collect_target_coverage_records(
    payloads: list[object],
    *,
    workspace_root: str | Path | None = None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    base = _base_path(workspace_root)
    for payload in payloads:
        _collect_records(payload, records, base)
    return _unique_records(_merge_read_file_windows(records))


def target_coverage_status(
    contract: dict[str, Any],
    *,
    coverage_records: list[dict[str, object]],
    workspace_root: str | Path | None = None,
) -> dict[str, object]:
    base = _base_path(workspace_root)
    targets = _target_items(contract)
    missing = [
        item
        for item in targets
        if not _target_is_covered(item, coverage_records, contract=contract, base=base)
    ]
    enforcement = _coverage_enforcement(contract, targets)
    should_block = bool(missing) and enforcement == "required"
    repair_hints = _repair_hints(missing, coverage_records, base=base)
    return {
        "scope_label": str(contract.get("scope_label") or ""),
        "enforcement": enforcement or "advisory",
        "expected_count": len(targets),
        "covered_count": len(targets) - len(missing),
        "missing_count": len(missing),
        "missing_items": missing[:50],
        "repair_hints": repair_hints[:50],
        "coverage_records": coverage_records[:100],
        "should_block": should_block,
        "recommended_next_action": _recommended_next_action(should_block, repair_hints),
    }


def _collect_records(value: object, records: list[dict[str, object]], base: Path | None) -> None:
    if isinstance(value, dict):
        _collect_from_mapping(value, records, base)
    for child in _children(value):
        _collect_records(child, records, base)


def _children(value: object) -> list[object]:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _collect_from_mapping(value: dict[str, object], records: list[dict[str, object]], base: Path | None) -> None:
    records.extend(_direct_coverage_records(value.get("coverage_records")))
    records.extend(_registry_coverage_records(value.get("artifact_registry_refs")))
    if record := _tool_call_coverage_record(value, base):
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
    raw = contract.get("target_items")
    if not isinstance(raw, list):
        return []
    return [row for item in raw if (row := _target_item_row(item))]


def _target_item_row(item: object) -> dict[str, str]:
    if not isinstance(item, dict):
        return {}
    target_id = str(item.get("target_id") or "").strip()
    row = {"target_id": target_id, "label": str(item.get("label") or target_id)} if target_id else {}
    for key in ("source_path", "artifact_ref", "source_ref", "coverage_kind", "enforcement", "scope"):
        text = str(item.get(key) or "").strip()
        if text:
            row[key] = text
    return row


def _record_counts_as_covered(record: dict[str, object]) -> bool:
    status = str(record.get("status") or "covered").strip()
    return bool(str(record.get("target_id") or "").strip()) and status == "covered"


def _tool_call_coverage_record(value: dict[str, object], base: Path | None) -> dict[str, object]:
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
    record: dict[str, object] = {
        "target_id": _canonical_ref(source, base),
        "status": "covered",
        "source_ref": source,
        "tool": tool,
    }
    if tool == "read_file":
        record.update(_read_file_window_fields(value, source, base))
    return record


def _read_file_window_fields(value: dict[str, object], source: str, base: Path | None) -> dict[str, object]:
    del source, base
    if fields := _structured_read_window_fields(value):
        return fields
    return {}


def _structured_read_window_fields(value: dict[str, object]) -> dict[str, object]:
    window = value.get("read_window")
    if not isinstance(window, dict):
        envelope = value.get("tool_result_envelope")
        window = envelope.get("read_window") if isinstance(envelope, dict) else {}
    if not isinstance(window, dict):
        return {}
    kind = str(window.get("kind") or "").strip()
    if kind == "char_window":
        offset = _optional_positive_or_zero_int(window.get("offset"))
        next_offset = _optional_positive_or_zero_int(window.get("next_offset"))
        total = _optional_positive_or_zero_int(window.get("total_chars"))
        if offset is None or next_offset is None or total is None:
            return {}
        return {
            "coverage_kind": "char_window",
            "status": "covered" if offset == 0 and next_offset >= total else "partial",
            "start_offset": offset,
            "end_offset": next_offset,
            "total_chars": total,
        }
    if kind == "line_window":
        start = _optional_positive_int(window.get("start_line"))
        end = _optional_positive_or_zero_int(window.get("end_line"))
        total = _optional_positive_or_zero_int(window.get("total_lines"))
        if start <= 0 or end is None or total is None:
            return {}
        return {
            "coverage_kind": "line_window",
            "status": "covered" if total > 0 and start == 1 and end >= total else "partial",
            "start_line": start,
            "end_line": end,
            "total_lines": total,
        }
    return {}


def _optional_positive_int(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _optional_positive_or_zero_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _merge_read_file_windows(records: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for record in records:
        if _is_window_record(record):
            groups.setdefault(_window_group_key(record), []).append(record)
    if not groups:
        return records
    merged = [_merged_window_record(items) for items in groups.values()]
    passthrough = [record for record in records if _should_keep_record(record, groups)]
    return [*merged, *passthrough]


def _should_keep_record(
    record: dict[str, object],
    window_groups: dict[tuple[str, str, str], list[dict[str, object]]],
) -> bool:
    if _is_window_record(record):
        return False
    if record.get("tool") != "read_file":
        return True
    return not any(_record_source_key(record) == key[1:] for key in window_groups)


def _merged_window_record(records: list[dict[str, object]]) -> dict[str, object]:
    if records[0].get("coverage_kind") == "line_window":
        return _merged_line_window_record(records)
    return _merged_char_window_record(records)


def _merged_char_window_record(records: list[dict[str, object]]) -> dict[str, object]:
    first = records[0]
    total = max(_int_record_value(record, "total_chars") for record in records)
    ranges = _merged_ranges(_window_ranges(records))
    covered_until = _covered_prefix_end(ranges)
    return {
        "target_id": str(first.get("target_id") or ""),
        "status": "covered" if total > 0 and covered_until >= total else "partial",
        "source_ref": str(first.get("source_ref") or ""),
        "tool": "read_file",
        "coverage_kind": "char_window",
        "covered_until_offset": covered_until,
        "total_chars": total,
        "read_ranges": [{"start": start, "end": end} for start, end in ranges[:20]],
    }


def _merged_line_window_record(records: list[dict[str, object]]) -> dict[str, object]:
    first = records[0]
    total = max(_int_record_value(record, "total_lines") for record in records)
    ranges = _merged_ranges(_line_ranges(records))
    covered_until = _covered_line_prefix_end(ranges)
    return {
        "target_id": str(first.get("target_id") or ""),
        "status": "covered" if total > 0 and covered_until >= total else "partial",
        "source_ref": str(first.get("source_ref") or ""),
        "tool": "read_file",
        "coverage_kind": "line_window",
        "covered_until_line": covered_until,
        "total_lines": total,
        "read_ranges": [{"start": start, "end": end} for start, end in ranges[:20]],
    }


def _is_window_record(record: dict[str, object]) -> bool:
    return record.get("coverage_kind") in {"char_window", "line_window"}


def _window_group_key(record: dict[str, object]) -> tuple[str, str, str]:
    target_id, source_ref = _record_source_key(record)
    return (str(record.get("coverage_kind") or ""), target_id, source_ref)


def _record_source_key(record: dict[str, object]) -> tuple[str, str]:
    return (
        str(record.get("target_id") or "").strip(),
        str(record.get("source_ref") or "").strip(),
    )


def _window_ranges(records: list[dict[str, object]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for record in records:
        start = _int_record_value(record, "start_offset")
        end = _int_record_value(record, "end_offset")
        if end > start:
            ranges.append((start, end))
    return ranges


def _line_ranges(records: list[dict[str, object]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for record in records:
        start = _int_record_value(record, "start_line")
        end = _int_record_value(record, "end_line")
        if end >= start > 0:
            ranges.append((start, end))
    return ranges


def _merged_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _covered_prefix_end(ranges: list[tuple[int, int]]) -> int:
    cursor = 0
    for start, end in ranges:
        if start > cursor:
            break
        cursor = max(cursor, end)
    return cursor


def _covered_line_prefix_end(ranges: list[tuple[int, int]]) -> int:
    cursor = 0
    for start, end in ranges:
        if start > cursor + 1:
            break
        cursor = max(cursor, end)
    return cursor


def _int_record_value(record: dict[str, object], key: str) -> int:
    try:
        return int(record.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _target_is_covered(
    item: dict[str, str],
    records: list[dict[str, object]],
    *,
    contract: dict[str, Any],
    base: Path | None,
) -> bool:
    item_keys = _target_keys(item, base)
    for record in records:
        if not item_keys.intersection(_coverage_keys(record, base)):
            continue
        if _target_requires_full_source_read(item, contract):
            if _full_source_read_record_counts(record):
                return True
            continue
        if _record_counts_as_covered(record):
            return True
    return False


def _target_requires_full_source_read(item: dict[str, str], contract: dict[str, Any]) -> bool:
    return (
        str(item.get("coverage_kind") or "").strip() == "full_source_read"
        or str(contract.get("coverage_requirement") or "").strip() == "full_source_read"
    )


def _coverage_enforcement(contract: dict[str, Any], targets: list[dict[str, str]]) -> str:
    top_level = str(contract.get("enforcement") or "").strip().lower()
    if top_level == "required":
        return "required"
    if top_level == "advisory":
        return top_level
    if top_level:
        return "advisory"
    item_values = [str(item.get("enforcement") or "").strip().lower() for item in targets]
    if any(value == "required" for value in item_values):
        return "required"
    return "advisory"


def _full_source_read_record_counts(record: dict[str, object]) -> bool:
    return (
        str(record.get("tool") or "").strip() == "read_file"
        and record.get("coverage_kind") in {"char_window", "line_window"}
        and _record_counts_as_covered(record)
    )


def _target_keys(item: dict[str, str], base: Path | None = None) -> set[str]:
    keys: set[str] = set()
    for key in ("target_id", "path", "source_path", "artifact_ref", "source_ref"):
        _add_ref_keys(keys, item.get(key), base)
    return keys


def _coverage_keys(record: dict[str, object], base: Path | None = None) -> set[str]:
    keys: set[str] = set()
    for key in ("target_id", "artifact_ref", "source_ref", "path"):
        _add_ref_keys(keys, record.get(key), base)
    return keys


def _repair_hints(
    missing: list[dict[str, str]],
    coverage_records: list[dict[str, object]],
    *,
    base: Path | None = None,
) -> list[dict[str, object]]:
    hints: list[dict[str, object]] = []
    for item in missing:
        if hint := _partial_read_hint(item, coverage_records, base):
            hints.append(hint)
    return hints


def _partial_read_hint(
    item: dict[str, str],
    coverage_records: list[dict[str, object]],
    base: Path | None = None,
) -> dict[str, object]:
    item_keys = _target_keys(item, base)
    for record in coverage_records:
        if record.get("coverage_kind") not in {"char_window", "line_window"}:
            continue
        if not item_keys.intersection(_coverage_keys(record, base)):
            continue
        source_ref = str(record.get("source_ref") or item.get("source_ref") or item.get("target_id") or "")
        if record.get("coverage_kind") == "char_window":
            offset = _int_record_value(record, "covered_until_offset")
            return {
                "target_id": str(item.get("target_id") or ""),
                "source_ref": source_ref,
                "covered_until_offset": offset,
                "total_chars": _int_record_value(record, "total_chars"),
                "recommended_tool_call": {"tool": "read_file", "path": source_ref, "offset": offset},
            }
        line = _int_record_value(record, "covered_until_line")
        return {
            "target_id": str(item.get("target_id") or ""),
            "source_ref": source_ref,
            "covered_until_line": line,
            "total_lines": _int_record_value(record, "total_lines"),
            "recommended_tool_call": {"tool": "read_file", "path": source_ref, "start_line": line + 1},
        }
    return {}


def _recommended_next_action(should_block: bool, repair_hints: list[dict[str, object]]) -> str:
    if not should_block:
        return "continue_or_summarize_with_missing_items_visible"
    if repair_hints:
        return "continue_read_file_from_repair_hints_then_submit"
    return "cover_missing_targets_before_submit"


def _add_ref_keys(keys: set[str], value: object, base: Path | None = None) -> None:
    text = str(value or "").strip()
    if not text:
        return
    keys.add(text)
    keys.add(_canonical_ref(text, base))


def _canonical_ref(value: object, base: Path | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text
    try:
        path = Path(text).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        return str(path.resolve(strict=False))
    except OSError:
        return text


def _base_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except OSError:
        return None


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
