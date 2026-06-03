from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report
from .home_layout import MyAgentHomePaths, home_paths


@dataclass(frozen=True)
class DailyMemoryQuery:
    query: str = ""
    date_key: str | None = None
    role: str = ""
    kind: str = ""
    limit: int = 20


@dataclass(frozen=True)
class DailyMemoryRecordsReport:
    records: list[dict[str, Any]]
    load_errors: list[dict[str, object]]


def read_daily_memory_records(paths: MyAgentHomePaths | str | Path, request: DailyMemoryQuery) -> list[dict[str, Any]]:
    return read_daily_memory_records_report(paths, request).records


def read_daily_memory_records_report(paths: MyAgentHomePaths | str | Path, request: DailyMemoryQuery) -> DailyMemoryRecordsReport:
    home = _coerce_home_paths(paths)
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    for path in daily_memory_files(home, request.date_key):
        report = _read_daily_file_report(path, request)
        records.extend(report.records)
        load_errors.extend(report.load_errors)
        if _limit_reached(records, request.limit):
            break
    return DailyMemoryRecordsReport(_apply_limit(records, request.limit), load_errors)


def daily_memory_files(paths: MyAgentHomePaths, date_key: str | None) -> list[Path]:
    return _jsonl_files_from_dirs(_daily_memory_dirs(paths), date_key)


def _coerce_home_paths(paths: MyAgentHomePaths | str | Path) -> MyAgentHomePaths:
    if isinstance(paths, MyAgentHomePaths):
        return paths
    return home_paths(paths)


def _daily_memory_dirs(paths: MyAgentHomePaths) -> tuple[Path, ...]:
    owner_daily = getattr(paths, "owner_memory_daily_dir", None)
    dirs = [Path(owner_daily)] if owner_daily else []
    if _is_local_main_owner(paths):
        dirs.append(paths.memory_daily_dir)
    return tuple(dict.fromkeys(dirs))


def _read_daily_file_report(path: Path, request: DailyMemoryQuery) -> DailyMemoryRecordsReport:
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    date_key = path.stem
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return DailyMemoryRecordsReport([], [_daily_memory_load_error(path, exc, line_no=0)])
    for line_no, line in enumerate(lines, start=1):
        obj, load_error = _parse_json_line_report(line, path=path, line_no=line_no)
        if load_error is not None:
            load_errors.append(load_error)
        if not obj or not _daily_record_matches(obj, request):
            continue
        obj["date"] = date_key
        obj["path"] = str(path)
        obj["line_no"] = line_no
        records.append(obj)
    return DailyMemoryRecordsReport(records, load_errors)


def _daily_record_matches(record: dict[str, Any], request: DailyMemoryQuery) -> bool:
    if request.role and str(record.get("role") or "") != request.role:
        return False
    if request.kind and str(record.get("kind") or "") != request.kind:
        return False
    return _text_contains(record, request.query)


def _jsonl_files_from_dirs(directories: tuple[Path, ...], date_key: str | None) -> list[Path]:
    files: list[Path] = []
    for directory in directories:
        if date_key:
            files.extend(_dated_jsonl_file(directory, date_key))
            continue
        files.extend(_all_jsonl_files(directory))
    return sorted(dict.fromkeys(files), reverse=True)


def _dated_jsonl_file(directory: Path, date_key: str) -> list[Path]:
    path = directory / f"{date_key}.jsonl"
    return [path] if path.exists() and path.is_file() else []


def _all_jsonl_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [path for path in directory.glob("*.jsonl") if path.is_file()]


def _parse_json_line_report(line: str, *, path: Path, line_no: int) -> tuple[dict[str, Any], dict[str, object] | None]:
    if not line.strip():
        return {}, None
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        return {}, _daily_memory_load_error(path, exc, line_no=line_no)
    if isinstance(value, dict):
        return value, None
    return {}, _daily_memory_load_error(
        path,
        ValueError(f"JSONL row is {type(value).__name__}, expected object"),
        line_no=line_no,
    )


def _daily_memory_load_error(path: Path, exc: BaseException, *, line_no: int) -> dict[str, object]:
    report = runtime_error_report(exc, context="home_runtime_query.daily_memory")
    report["path"] = str(path)
    if line_no:
        report["line"] = line_no
    return report


def _is_local_main_owner(paths: MyAgentHomePaths) -> bool:
    return (
        str(getattr(paths, "owner_provider", "") or "local") == "local"
        and str(getattr(paths, "owner_kind", "") or "main") == "main"
        and str(getattr(paths, "owner_id", "") or "local/main") == "local/main"
    )


def _text_contains(value: dict[str, Any], query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return True
    return text in json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


def _limit_reached(items: list[dict[str, Any]], limit: int) -> bool:
    return int(limit or 0) > 0 and len(items) >= int(limit)


def _apply_limit(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    value = int(limit or 0)
    return items[:value] if value > 0 else items


__all__ = [
    "DailyMemoryQuery",
    "DailyMemoryRecordsReport",
    "daily_memory_files",
    "read_daily_memory_records",
    "read_daily_memory_records_report",
]
