"""LLM: Parse artifact-registry JSONL supplied by a trusted storage seam.

模块用途: 把账本文本解析成最新产物记录并保留坏行诊断；文件如何安全读取由上层 registry 决定。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.json_io import read_text_lines_cached
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
    return latest_records_from_lines(
        path,
        lines,
        record_from_payload,
        initial_errors=context.errors,
    )


# LLM: Host-owned callers may obtain bytes through descriptor-anchored IO. Keep parsing separate so
# those callers never have to reopen the mutable workspace path with Path APIs.
# 函数用途: 解析调用方已经安全读取的 JSONL 行，并返回每个 artifact_id 的最新记录和坏行诊断。
def latest_records_from_lines(
    path: Path,
    lines: list[str],
    record_from_payload: Callable[[object], Any],
    *,
    initial_errors: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = RegistryReadContext(
        path=path,
        record_from_payload=record_from_payload,
        errors=list(initial_errors or []),
    )
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
