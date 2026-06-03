
from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report
from .models import ObservationEvent


@dataclass(frozen=True)
class JsonlReadReport:
    rows: list[dict[str, Any]]
    load_errors: list[dict[str, Any]]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return read_jsonl_report(path, context="conversation.jsonl").rows


def read_jsonl_report(path: Path, *, context: str) -> JsonlReadReport:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return JsonlReadReport([], [_jsonl_error(exc, context, path=path)])
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        row, error = _json_row(line, context=context, path=path, line_number=line_number)
        if error is not None:
            errors.append(error)
            continue
        if row is not None:
            rows.append(row)
    return JsonlReadReport(rows, errors)


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)


def float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def safe_file_stem(value: str) -> str:
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text) or "unknown"


def wake_urgency(value: str) -> str:
    return "urgent" if str(value or "").strip().lower() == "urgent" else "normal"


def with_handled_at(event: ObservationEvent, handled: dict[str, float]) -> ObservationEvent:
    handled_at = float(handled.get(event.observation_id) or event.handled_at or 0.0)
    return event if handled_at == event.handled_at else replace(event, handled_at=handled_at)


def wake_evidence_refs(observation: ObservationEvent | None, explicit_refs: object) -> tuple[str, ...]:
    if explicit_refs is not None:
        refs = explicit_refs
    elif observation is not None:
        refs = observation.evidence_refs
    else:
        refs = ()
    return tuple(str(item) for item in refs if str(item or "").strip()) if isinstance(refs, (list, tuple)) else ()


def _json_row(
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
