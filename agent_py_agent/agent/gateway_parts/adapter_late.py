from __future__ import annotations

"""late gateway responses for file adapter requests."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report
from .logging import _report_gateway_side_effect_error
from .paths import AdapterPaths


@dataclass(frozen=True)
class LateResponseCheckReport:
    results: list[dict]
    load_errors: list[dict]


@dataclass(frozen=True)
class _LatePendingEntriesReport:
    entries: list[dict]
    raw_unreadable_lines: list[str]
    load_errors: list[dict]


def _record_late_pending(paths: AdapterPaths, request_id: str, original_timeout: float) -> None:
    late_path = paths.root / "late_pending.jsonl"
    entry = {
        "request_id": request_id,
        "timeout_at": time.time(),
        "original_timeout": original_timeout,
        "checked": False,
    }
    try:
        with late_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _report_gateway_side_effect_error("record_late_pending", request_id, exc)


def check_late_responses(paths: AdapterPaths) -> list[dict]:
    return check_late_responses_report(paths).results


def check_late_responses_report(paths: AdapterPaths) -> LateResponseCheckReport:
    late_path = paths.root / "late_pending.jsonl"
    if not late_path.exists():
        return LateResponseCheckReport([], [])
    results: list[dict] = []
    remaining: list[dict] = []
    entries_report = _iter_late_pending_entries_report(late_path)
    for entry in entries_report.entries:
        updated = _mark_late_response_if_arrived(paths, entry)
        if updated.get("checked"):
            results.append(updated)
        remaining.append(updated)
    _rewrite_unchecked_late_entries(late_path, remaining, entries_report.raw_unreadable_lines)
    return LateResponseCheckReport(results, entries_report.load_errors)


def _iter_late_pending_entries(late_path: Path) -> list[dict]:
    return _iter_late_pending_entries_report(late_path).entries


def _iter_late_pending_entries_report(late_path: Path) -> _LatePendingEntriesReport:
    entries: list[dict] = []
    raw_unreadable_lines: list[str] = []
    load_errors: list[dict] = []
    try:
        lines = late_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return _LatePendingEntriesReport([], [], [_late_pending_load_error(late_path, exc)])
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raw_unreadable_lines.append(line)
            load_errors.append(_late_pending_load_error(late_path, exc, line_number=line_number))
            continue
        if not isinstance(entry, dict):
            raw_unreadable_lines.append(line)
            load_errors.append(
                _late_pending_load_error(
                    late_path,
                    ValueError(f"late pending row is {type(entry).__name__}, expected object"),
                    line_number=line_number,
                )
            )
            continue
        if not entry.get("checked"):
            entries.append(entry)
    return _LatePendingEntriesReport(entries, raw_unreadable_lines, load_errors)


def _late_pending_load_error(
    late_path: Path,
    exc: BaseException,
    *,
    line_number: int = 0,
) -> dict[str, Any]:
    report = runtime_error_report(exc, context="gateway.adapter_late_pending.read")
    report["path"] = str(late_path)
    if line_number:
        report["line_number"] = line_number
    return report


def _mark_late_response_if_arrived(paths: AdapterPaths, entry: dict) -> dict:
    request_id = entry.get("request_id", "")
    gateway_responses_dir = paths.root.parent / "gateway" / "responses"
    response_path = gateway_responses_dir / f"{request_id}.json"
    if response_path.exists():
        entry["checked"] = True
        entry["late_response_at"] = time.time()
    return entry


def _rewrite_unchecked_late_entries(late_path: Path, entries: list[dict], raw_unreadable_lines: list[str] | None = None) -> None:
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries if not entry.get("checked")]
    lines.extend(raw_unreadable_lines or [])
    content = "\n".join(lines) + ("\n" if lines else "")
    try:
        late_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _report_gateway_side_effect_error("check_late_responses_cleanup", "", exc)
