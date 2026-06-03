"""Small JSON IO helpers for runtime metadata files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report


@dataclass(frozen=True)
class JsonObjectReadReport:
    payload: dict[str, Any]
    load_error: dict[str, object] | None = None


@dataclass(frozen=True)
class JsonlObjectsReadReport:
    records: list[dict[str, Any]]
    load_errors: list[dict[str, object]]


def read_json_object(path: Path, *, parse_nested_string: bool = False) -> dict[str, Any]:
    """Read a JSON object, returning an empty dict for missing or malformed files."""

    return read_json_object_report(path, parse_nested_string=parse_nested_string).payload


def read_json_object_report(
    path: Path,
    *,
    parse_nested_string: bool = False,
    context: str = "json_io.read_json_object",
) -> JsonObjectReadReport:
    """Read a JSON object and preserve a model-visible error for malformed files."""

    if not path.exists():
        return JsonObjectReadReport({})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if parse_nested_string and isinstance(payload, str):
            payload = json.loads(payload)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonObjectReadReport({}, _json_object_load_error(path, exc, context))
    if isinstance(payload, dict):
        return JsonObjectReadReport(payload)
    return JsonObjectReadReport(
        {},
        _json_object_load_error(path, ValueError(f"JSON root is {type(payload).__name__}, expected object"), context),
    )


def _json_object_load_error(path: Path, exc: BaseException, context: str) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


def write_json_object(path: Path, payload: dict[str, object], *, sort_keys: bool = True) -> None:
    """Write a small JSON object with parent creation and a trailing newline."""

    write_json_file(path, payload, sort_keys=sort_keys)


def write_json_file(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    """Write JSON payloads with parent creation and a trailing newline."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n",
        encoding="utf-8",
    )


def read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    """Read JSONL objects, skipping blank or malformed rows."""

    return read_jsonl_objects_report(path).records


def read_jsonl_objects_report(path: Path, *, context: str = "json_io.read_jsonl_objects") -> JsonlObjectsReadReport:
    """Read JSONL objects and preserve recoverable diagnostics for bad rows."""

    if not path.exists():
        return JsonlObjectsReadReport([], [])
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return JsonlObjectsReadReport([], [_jsonl_load_error(path, exc, context, line_no=0)])
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            load_errors.append(_jsonl_load_error(path, exc, context, line_no=line_no))
            continue
        if isinstance(payload, dict):
            records.append(payload)
            continue
        load_errors.append(
            _jsonl_load_error(
                path,
                ValueError(f"JSONL row is {type(payload).__name__}, expected object"),
                context,
                line_no=line_no,
            )
        )
    return JsonlObjectsReadReport(records, load_errors)


def _jsonl_load_error(path: Path, exc: BaseException, context: str, *, line_no: int) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    if line_no:
        report["line"] = line_no
    return report


def write_jsonl_records(path: Path, records: list[dict[str, object]], *, sort_keys: bool = True) -> None:
    """Write a JSONL file, replacing existing content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=sort_keys) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


def append_jsonl_records(path: Path, records: list[dict[str, object]], *, sort_keys: bool = True) -> None:
    """Append JSONL records, doing nothing for an empty batch."""

    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=sort_keys) + "\n")
