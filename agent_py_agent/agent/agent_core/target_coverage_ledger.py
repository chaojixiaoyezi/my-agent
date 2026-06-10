from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_SOURCE_CANDIDATE_SCAN = 2000
_MAX_SOURCE_CANDIDATES = 40
_IGNORED_SOURCE_DIR_NAMES = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "site-packages",
    "target",
    "vendor",
    "venv",
}
_SOURCE_CANDIDATE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".md",
    ".mjs",
    ".mts",
    ".php",
    ".py",
    ".rs",
    ".rst",
    ".sh",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
_CODE_CANDIDATE_SUFFIXES = _SOURCE_CANDIDATE_SUFFIXES - {".md", ".rst", ".toml", ".json", ".yaml", ".yml"}
_SOURCE_CANDIDATE_NAMES = {
    "Cargo.toml",
    "go.mod",
    "package.json",
    "pyproject.toml",
    "README",
    "README.md",
    "README.rst",
    "requirements.txt",
    "setup.py",
}
_ENTRYLIKE_STEMS = {
    "__init__",
    "agent",
    "app",
    "assistant",
    "cli",
    "client",
    "index",
    "lib",
    "main",
    "mod",
    "server",
}
_SOURCE_DIR_PARTS = {"app", "cmd", "crates", "lib", "packages", "src"}
_ROOT_README_NAMES = {"README", "README.md", "README.rst"}


@dataclass(frozen=True)
class CoverageRecordMatchRequest:
    record: dict[str, object]
    item: dict[str, str]
    item_keys: set[str]
    contract: dict[str, Any]
    base: Path | None


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
        "target_items": targets[:100],
        "coverage_records": coverage_records[:100],
        "source_fact_records": _source_fact_records(coverage_records),
        "should_block": should_block,
        "recommended_next_action": _recommended_next_action(should_block, repair_hints),
    }


def _source_fact_records(coverage_records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        dict(record)
        for record in coverage_records
        if str(record.get("tool") or "").strip() == "read_file"
        and str(record.get("status") or "").strip() == "covered"
        and str(record.get("source_ref") or record.get("target_id") or "").strip()
    ]


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
    for key in (
        "source_path", "artifact_ref", "source_ref", "coverage_kind", "enforcement",
        "scope", "min_read_count", "min_read_ratio", "max_candidates",
    ):
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
    if created_at := str(value.get("created_at") or "").strip():
        record["created_at"] = created_at
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
        **_latest_created_at_field(records),
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
        **_latest_created_at_field(records),
    }


def _latest_created_at_field(records: list[dict[str, object]]) -> dict[str, object]:
    values = sorted(
        text
        for record in records
        if (text := str(record.get("created_at") or "").strip())
    )
    return {"created_at": values[-1]} if values else {}


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
    if _target_requires_source_file_under_dir(item):
        return _source_file_under_dir_covered(item, records, base)
    if _target_requires_directory_tree(item):
        return _directory_tree_covered(item, records, base)
    item_keys = _target_keys(item, base)
    for record in records:
        if _record_covers_target(CoverageRecordMatchRequest(record, item, item_keys, contract, base)):
            return True
    return False


def _record_covers_target(request: CoverageRecordMatchRequest) -> bool:
    if not request.item_keys.intersection(_coverage_keys(request.record, request.base)):
        return False
    if _target_requires_full_source_read(request.item, request.contract):
        return _full_source_read_record_counts(request.record)
    return _record_counts_as_covered(request.record)


def _target_requires_full_source_read(item: dict[str, str], contract: dict[str, Any]) -> bool:
    return (
        str(item.get("coverage_kind") or "").strip() == "full_source_read"
        or str(contract.get("coverage_requirement") or "").strip() == "full_source_read"
    )


def _target_requires_source_file_under_dir(item: dict[str, str]) -> bool:
    return str(item.get("coverage_kind") or "").strip() == "source_file_under_dir"


def _source_file_under_dir_covered(
    item: dict[str, str],
    records: list[dict[str, object]],
    base: Path | None,
) -> bool:
    source = str(item.get("source_ref") or item.get("source_path") or item.get("target_id") or "").strip()
    if not source:
        return False
    target = _canonical_ref(source, base)
    return len(_read_files_under_target(target, records, base)) >= _effective_min_read_count(item, target, base)


def _target_requires_directory_tree(item: dict[str, str]) -> bool:
    return str(item.get("coverage_kind") or "").strip() == "directory_tree"


def _directory_tree_covered(
    item: dict[str, str],
    records: list[dict[str, object]],
    base: Path | None,
) -> bool:
    """目录树级覆盖：候选源文件中已读比例达到 min_read_ratio（默认 1.0 全读）。

    候选集合来自源文件扫描（带上限）；合同可用 max_candidates 显式扩大上限。
    候选为空（目录没有源文件）按已覆盖处理，但 hint 会暴露 candidate_scan_truncated。"""
    source = str(item.get("source_ref") or item.get("source_path") or item.get("target_id") or "").strip()
    if not source:
        return False
    target = _canonical_ref(source, base)
    candidates, _truncated = _directory_tree_candidates(item, target, base)
    if not candidates:
        return True
    read_files = _read_files_under_target(target, records, base)
    required = _directory_tree_required_count(item, len(candidates))
    return len(read_files.intersection(candidates)) >= required


def _directory_tree_candidates(
    item: dict[str, str],
    target: str,
    base: Path | None,
) -> tuple[set[str], bool]:
    max_candidates = _declared_positive_int(item.get("max_candidates"), default=_MAX_SOURCE_CANDIDATES)
    candidates = _source_file_candidates_under_target(target, base, max_candidates=max_candidates)
    truncated = len(candidates) >= max_candidates
    return set(candidates), truncated


def _directory_tree_required_count(item: dict[str, str], candidate_count: int) -> int:
    ratio = _declared_ratio(item.get("min_read_ratio"), default=1.0)
    from math import ceil

    return max(1, min(candidate_count, ceil(candidate_count * ratio)))


def _declared_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _declared_ratio(value: object, *, default: float) -> float:
    try:
        parsed = float(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if parsed <= 0 or parsed > 1:
        return default
    return parsed


def _read_files_under_target(
    target: str,
    coverage_records: list[dict[str, object]],
    base: Path | None = None,
) -> set[str]:
    read_files: set[str] = set()
    for record in coverage_records:
        if str(record.get("tool") or "").strip() != "read_file":
            continue
        record_source = str(record.get("source_ref") or record.get("target_id") or "").strip()
        if not record_source:
            continue
        canonical = _canonical_ref(record_source, base)
        if canonical == target or canonical.startswith(target.rstrip("/\\") + "/"):
            read_files.add(canonical)
    return read_files


def _min_read_count(item: dict[str, str]) -> int:
    try:
        return max(1, int(str(item.get("min_read_count") or "1").strip()))
    except ValueError:
        return 1


def _effective_min_read_count(item: dict[str, str], target: str, base: Path | None = None) -> int:
    requested = _min_read_count(item)
    candidates = _source_file_candidates_under_target(target, base)
    if not candidates:
        return requested
    return max(1, min(requested, len(candidates)))


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
        if hint := _source_file_under_dir_hint(item, coverage_records, base):
            hints.append(hint)
            continue
        if hint := _directory_tree_hint(item, coverage_records, base):
            hints.append(hint)
            continue
        if hint := _partial_read_hint(item, coverage_records, base):
            hints.append(hint)
    return hints


def _source_file_under_dir_hint(
    item: dict[str, str],
    coverage_records: list[dict[str, object]],
    base: Path | None = None,
) -> dict[str, object]:
    if not _target_requires_source_file_under_dir(item):
        return {}
    source = str(item.get("source_ref") or item.get("source_path") or item.get("target_id") or "").strip()
    if not source:
        return {}
    target = _canonical_ref(source, base)
    read_files = sorted(_read_files_under_target(target, coverage_records, base))
    candidate_files = [
        candidate
        for candidate in _source_file_candidates_under_target(target, base)
        if candidate not in read_files
    ]
    if read_files:
        candidate_files = sorted(candidate_files, key=_repair_candidate_sort_key)
    needed = max(1, _effective_min_read_count(item, target, base) - len(read_files))
    recommended_read_calls = [
        {"tool": "read_file", "path": candidate}
        for candidate in candidate_files[:needed]
    ]
    recommended = (
        recommended_read_calls[0]
        if recommended_read_calls
        else {"tool": "list_files", "path": source, "recursive": True, "limit": 50}
    )
    return {
        "target_id": str(item.get("target_id") or ""),
        "source_ref": source,
        "coverage_kind": "source_file_under_dir",
        "current_read_count": len(read_files),
        "min_read_count": _effective_min_read_count(item, target, base),
        "read_files": read_files[:20],
        "candidate_read_files": candidate_files[:10],
        "recommended_tool_call": recommended,
        "recommended_tool_calls": recommended_read_calls,
    }


def _directory_tree_hint(
    item: dict[str, str],
    coverage_records: list[dict[str, object]],
    base: Path | None = None,
) -> dict[str, object]:
    if not _target_requires_directory_tree(item):
        return {}
    source = str(item.get("source_ref") or item.get("source_path") or item.get("target_id") or "").strip()
    if not source:
        return {}
    target = _canonical_ref(source, base)
    candidates, truncated = _directory_tree_candidates(item, target, base)
    read_files = _read_files_under_target(target, coverage_records, base)
    missing = sorted(candidates - read_files)
    required = _directory_tree_required_count(item, len(candidates)) if candidates else 0
    needed = max(0, required - len(read_files.intersection(candidates)))
    recommended_read_calls = [
        {"tool": "read_file", "path": candidate} for candidate in missing[:needed]
    ]
    return {
        "target_id": str(item.get("target_id") or ""),
        "source_ref": source,
        "coverage_kind": "directory_tree",
        "candidate_count": len(candidates),
        "candidate_scan_truncated": truncated,
        "read_count": len(read_files.intersection(candidates)),
        "required_read_count": required,
        "missing_files": missing[:20],
        "recommended_tool_call": (
            recommended_read_calls[0]
            if recommended_read_calls
            else {"tool": "list_files", "path": source, "recursive": True, "limit": 50}
        ),
        "recommended_tool_calls": recommended_read_calls,
    }


def _source_file_candidates_under_target(
    target: str,
    base: Path | None = None,
    *,
    max_candidates: int = _MAX_SOURCE_CANDIDATES,
) -> list[str]:
    path = _local_path_from_ref(target, base)
    if path is None or not path.is_dir():
        return []
    sorted_candidates = sorted(
        _source_candidate_paths(path),
        key=lambda candidate: _source_candidate_sort_key(candidate, path),
    )
    return [str(candidate.resolve(strict=False)) for candidate in sorted_candidates[:max_candidates]]


def _source_candidate_paths(path: Path) -> list[Path]:
    with suppress(OSError):
        return _source_candidate_paths_from_walk(path)
    return []


def _source_candidate_paths_from_walk(path: Path) -> list[Path]:
    candidates: list[Path] = []
    scanned = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = _scan_source_dirnames(dirs)
        scanned = _append_source_candidates(candidates, Path(root), files, scanned)
        if scanned > _MAX_SOURCE_CANDIDATE_SCAN:
            break
    return candidates


def _scan_source_dirnames(dirnames: list[str]) -> list[str]:
    return [
        dirname
        for dirname in dirnames
        if dirname not in _IGNORED_SOURCE_DIR_NAMES and not dirname.startswith(".")
    ]


def _append_source_candidates(candidates: list[Path], root: Path, files: list[str], scanned: int) -> int:
    for name in files:
        scanned += 1
        if scanned > _MAX_SOURCE_CANDIDATE_SCAN:
            break
        candidate = root / name
        if _looks_like_source_candidate(candidate):
            candidates.append(candidate)
    return scanned


def _local_path_from_ref(value: object, base: Path | None = None) -> Path | None:
    text = str(value or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        return path.resolve(strict=False)
    except OSError:
        return None


def _looks_like_source_candidate(path: Path) -> bool:
    name = path.name
    if name.endswith((".lock", ".map", ".min.js")):
        return False
    return name in _SOURCE_CANDIDATE_NAMES or path.suffix.lower() in _SOURCE_CANDIDATE_SUFFIXES


def _source_candidate_sort_key(path: Path, root: Path) -> tuple[int, int, int, str]:
    parts = _relative_parts(path, root)
    kind_rank = _source_candidate_kind_rank(path, parts)
    source_part_rank = 0 if any(part in _SOURCE_DIR_PARTS for part in parts) else 1
    entry_rank = 0 if path.stem in _ENTRYLIKE_STEMS else 1
    return (kind_rank, source_part_rank, entry_rank, "/".join(parts))


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def _source_candidate_kind_rank(path: Path, parts: tuple[str, ...]) -> int:
    name = path.name
    if name in _ROOT_README_NAMES and len(parts) == 1:
        return 0
    if name in {"package.json", "pyproject.toml", "go.mod", "Cargo.toml", "setup.py"}:
        return 1
    if path.suffix.lower() in _SOURCE_CANDIDATE_SUFFIXES - {".md", ".rst"}:
        return 2
    if name.startswith("README"):
        return 3
    return 4


def _repair_candidate_sort_key(value: str) -> tuple[int, str]:
    path = Path(value)
    suffix = path.suffix.lower()
    if suffix in _CODE_CANDIDATE_SUFFIXES:
        return (0, value)
    if path.name in {"package.json", "pyproject.toml", "go.mod", "Cargo.toml", "setup.py"}:
        return (1, value)
    return (2, value)


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
