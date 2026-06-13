
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import read_text_lines_cached
from ..runtime_errors import runtime_error_report


@dataclass(frozen=True)
class JsonlReadReport:
    rows: list[dict[str, Any]]
    load_errors: list[dict[str, Any]]


def strings(value: object) -> tuple[str, ...]:
    """Normalize optional open-world string lists without enum validation."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item or "").strip())


def dict_items(value: object) -> tuple[dict[str, Any], ...]:
    """Keep valid open-world clue/hint objects and ignore only dirty items."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read tolerant JSONL rows; malformed rows do not drop the whole ledger."""
    return read_jsonl_report(path, context="collaboration.jsonl").rows


def read_jsonl_report(path: Path, *, context: str) -> JsonlReadReport:
    """Read JSONL rows and report dirty rows without hiding valid rows."""
    try:
        # mtime+size 守门缓存(批4):轮询场景文件多数时刻没变,免重复磁盘读。
        lines = read_text_lines_cached(path)
    except FileNotFoundError:
        return JsonlReadReport([], [])
    except OSError as exc:
        return JsonlReadReport([], [_jsonl_error(exc, context, path=path)])
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        row, error = _json_object_row(line, context=context, path=path, line_number=line_number)
        if error is not None:
            errors.append(error)
            continue
        if row is not None:
            rows.append(row)
    return JsonlReadReport(rows, errors)


def now(value: float | None = None) -> float:
    """Return explicit test time or wall-clock time."""
    return float(time.time() if value is None else value)


def float_value(value: object) -> float:
    """Parse numeric metadata fields defensively."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _json_object_row(
    line: str,
    *,
    context: str,
    path: Path,
    line_number: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, _jsonl_error(exc, context, path=path, line_number=line_number)
    if not isinstance(row, dict):
        return None, _jsonl_error(
            ValueError(f"JSONL row is {type(row).__name__}, expected object"),
            context,
            path=path,
            line_number=line_number,
        )
    return row, None


def _jsonl_error(
    exc: BaseException,
    context: str,
    *,
    path: Path,
    line_number: int | None = None,
) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    if line_number is not None:
        report["line_number"] = line_number
    return report
