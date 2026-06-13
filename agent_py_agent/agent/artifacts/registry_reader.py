"""Diagnostic readers for artifact registry JSONL files."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..common.json_io import read_text_lines_cached
from typing import Any

from ..runtime_errors import runtime_error_report


@dataclass
class RegistryReadContext:
    """Mutable state for reading one registry file."""

    path: Path
    record_from_payload: Callable[[object], Any]
    latest: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)


def latest_records_with_errors(
    path: Path,
    record_from_payload: Callable[[object], Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return latest records by id and preserve malformed-row diagnostics."""

    context = RegistryReadContext(path=path, record_from_payload=record_from_payload)
    if not path.exists():
        return context.latest, context.errors
    try:
        # mtime+size 守门缓存(批4):注册表反复 lookup 时免整文件重读。
        lines = read_text_lines_cached(path)
    except (OSError, UnicodeError) as exc:
        context.errors.append(_error_report(exc, context="artifact_registry.read", path=path))
        return context.latest, context.errors
    for line_no, line in enumerate(lines, start=1):
        _read_registry_line(context, line, line_no=line_no)
    return context.latest, context.errors


def lookup_record_with_errors(
    records: dict[str, Any],
    errors: list[dict[str, Any]],
    artifact_id: str = "",
    *,
    path: str | Path = "",
) -> tuple[Any | None, list[dict[str, Any]]]:
    """Resolve a record by explicit id first, then exact resolved path."""

    key = str(artifact_id or "").strip()
    if key and key in records:
        return records[key], errors
    path_text = str(path or "").strip()
    if not path_text:
        return None, errors
    try:
        resolved = Path(path_text).expanduser().resolve(strict=False)
    except OSError as exc:
        return None, [*errors, _error_report(exc, context="artifact_registry.resolve_path", path=path_text)]
    for record in records.values():
        if Path(record.path).expanduser().resolve(strict=False) == resolved:
            return record, errors
    return None, errors


def _read_registry_line(context: RegistryReadContext, line: str, *, line_no: int) -> None:
    if not line.strip():
        return
    try:
        payload = json.loads(line)
        record = context.record_from_payload(payload)
        if record is None:
            raise ValueError("invalid artifact registry record")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        context.errors.append(_error_report(exc, context="artifact_registry.read_line", path=context.path, line=line_no))
        return
    if record.artifact_id:
        context.latest[record.artifact_id] = record


def _error_report(
    exc: BaseException,
    *,
    context: str,
    path: str | Path,
    line: int | None = None,
) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    if line is not None:
        report["line"] = line
    return report
