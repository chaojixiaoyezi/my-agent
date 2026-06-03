from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..runtime_errors import runtime_error_report


@dataclass(frozen=True)
class CapabilityIndexRecordsReport:
    records: list[dict[str, object]]
    load_errors: list[dict[str, object]]


def read_capability_index_records(path: Path, name: str, *, source: str) -> CapabilityIndexRecordsReport:
    if not path.exists():
        return CapabilityIndexRecordsReport([], [])
    rows: list[dict[str, object]] = []
    load_errors: list[dict[str, object]] = []
    wanted = name.lower()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return CapabilityIndexRecordsReport([], [_index_load_error(path, exc, line_no=0)])
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            load_errors.append(_index_load_error(path, exc, line_no=line_no))
            continue
        if not isinstance(payload, dict):
            load_errors.append(
                _index_load_error(
                    path,
                    ValueError(f"JSONL row is {type(payload).__name__}, expected object"),
                    line_no=line_no,
                )
            )
            continue
        label = str(payload.get("name") or payload.get("title") or payload.get("id") or "").strip()
        if label.lower() != wanted:
            continue
        row = dict(payload)
        row.setdefault("source", source)
        rows.append(row)
    return CapabilityIndexRecordsReport(rows, load_errors)


def dedupe_capability_index_load_errors(errors: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for error in errors:
        key = (
            str(error.get("context") or ""),
            str(error.get("path") or ""),
            str(error.get("line") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(error)
    return result


def _index_load_error(path: Path, exc: BaseException, *, line_no: int) -> dict[str, object]:
    report = runtime_error_report(exc, context="capability_resolver.index_row" if line_no else "capability_resolver.index_file")
    report["path"] = str(path)
    if line_no:
        report["line"] = line_no
    return report


__all__ = [
    "CapabilityIndexRecordsReport",
    "dedupe_capability_index_load_errors",
    "read_capability_index_records",
]
