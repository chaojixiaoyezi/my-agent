
from __future__ import annotations

"""read-only control-plane queries for runtime memory.

Human version:
The control plane is the small "where is the thing?" API. It does not load full
tool outputs or rewrite memory files. It only scans lightweight ledgers and
returns scoped references that compact/resume/debug flows can follow.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)
from .tool_output_externalizer import tool_output_index_paths_for_lookup

CONTROL_PLANE_QUERY_SCHEMA = RuntimeMemorySchemaOptions("control_plane_query")
CONTROL_PLANE_TASK_RUN_REF_SCHEMA = RuntimeMemorySchemaOptions("control_plane_task_run_ref")
CONTROL_PLANE_DECODE_ERROR_SCHEMA = RuntimeMemorySchemaOptions("control_plane_decode_error")


@dataclass(frozen=True)
class MemoryControlPlaneQueryOptions:
    """Bundle filters for a read-only memory control-plane query."""

    date: str = ""
    task_id: str = ""
    run_id: str = ""
    event_type: str = ""
    include_missing_refs: bool = True
    limit: int = 50


@dataclass(frozen=True)
class _ControlPlaneResultParts:
    workspace: Path
    options: MemoryControlPlaneQueryOptions
    daily_events: list[dict[str, Any]]
    task_run_refs: list[dict[str, Any]]
    compact_applies: list[dict[str, Any]]
    tool_outputs: list[dict[str, Any]]


def query_memory_control_plane(root: str | Path, options: MemoryControlPlaneQueryOptions) -> dict[str, Any]:
    workspace = Path(root)
    daily_events = _limited(_matching_records(_daily_events(workspace, options), options), options.limit)
    compact_applies = _limited(_matching_records(_compact_applies(workspace), options), options.limit)
    tool_outputs = _limited(_matching_records(_tool_outputs(workspace), options), options.limit)
    task_run_refs = _limited(_task_run_refs(daily_events, options), options.limit)
    return _result_payload(
        _ControlPlaneResultParts(workspace, options, daily_events, task_run_refs, compact_applies, tool_outputs)
    )


def _result_payload(parts: _ControlPlaneResultParts) -> dict[str, Any]:
    return {
        "version": CONTROL_PLANE_QUERY_SCHEMA.version,
        "schema": runtime_memory_schema_payload(CONTROL_PLANE_QUERY_SCHEMA),
        "ok": True,
        "workspace_root": str(parts.workspace),
        "scope": _scope_payload(parts.options),
        "daily_events": parts.daily_events,
        "task_run_refs": parts.task_run_refs,
        "compact_applies": parts.compact_applies,
        "tool_outputs": parts.tool_outputs,
        "counts": {
            "daily_events": len(parts.daily_events),
            "task_run_refs": len(parts.task_run_refs),
            "compact_applies": len(parts.compact_applies),
            "tool_outputs": len(parts.tool_outputs),
        },
    }


def _daily_events(workspace: Path, options: MemoryControlPlaneQueryOptions) -> list[dict[str, Any]]:
    paths = [workspace / "daily" / options.date / "events.jsonl"] if options.date else _daily_event_paths(workspace)
    return _read_many_jsonl(paths)


def _daily_event_paths(workspace: Path) -> list[Path]:
    return sorted((workspace / "daily").glob("*/events.jsonl"))


def _compact_applies(workspace: Path) -> list[dict[str, Any]]:
    return _read_jsonl(workspace / "memory_archive" / "compact_applies" / "ledger.jsonl")


def _tool_outputs(workspace: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in tool_output_index_paths_for_lookup(workspace):
        rows.extend(_read_jsonl(path))
    return rows


def _task_run_refs(
    daily_events: list[dict[str, Any]], options: MemoryControlPlaneQueryOptions
) -> list[dict[str, Any]]:
    refs_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for event in daily_events:
        ref = _task_run_ref(event, options.include_missing_refs)
        if ref:
            refs_by_key[(ref["task_id"], ref["run_id"])] = ref
    return list(refs_by_key.values())


def _task_run_ref(event: dict[str, Any], include_missing_refs: bool) -> dict[str, Any]:
    refs = event.get("refs") if isinstance(event.get("refs"), dict) else {}
    missing_refs = _missing_refs(refs)
    if missing_refs and not include_missing_refs:
        return {}
    return {
        "version": CONTROL_PLANE_TASK_RUN_REF_SCHEMA.version,
        "schema": runtime_memory_schema_payload(CONTROL_PLANE_TASK_RUN_REF_SCHEMA),
        "task_id": str(event.get("task_id") or ""),
        "run_id": str(event.get("run_id") or ""),
        "status": str(event.get("status") or ""),
        "progress": event.get("progress", 0.0),
        "summary": str(event.get("summary") or ""),
        "refs": dict(refs),
        "missing_refs": missing_refs,
    }


def _matching_records(
    records: list[dict[str, Any]], options: MemoryControlPlaneQueryOptions
) -> list[dict[str, Any]]:
    return [record for record in records if _matches_scope(record, options)]


def _matches_scope(record: dict[str, Any], options: MemoryControlPlaneQueryOptions) -> bool:
    return (
        _matches_value(_record_value(record, "task_id"), options.task_id)
        and _matches_value(_record_value(record, "run_id"), options.run_id)
        and _matches_value(str(record.get("event_type") or record.get("kind") or ""), options.event_type)
    )


def _record_value(record: dict[str, Any], key: str) -> str:
    scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
    return str(record.get(key) or scope.get(key) or "")


def _matches_value(actual: str, expected: str) -> bool:
    return not expected or actual == expected


def _scope_payload(options: MemoryControlPlaneQueryOptions) -> dict[str, Any]:
    return {
        "date": options.date,
        "task_id": options.task_id,
        "run_id": options.run_id,
        "event_type": options.event_type,
        "include_missing_refs": options.include_missing_refs,
        "limit": options.limit,
    }


def _missing_refs(refs: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key, value in refs.items():
        text = str(value or "")
        if text and _looks_like_path(text) and not Path(text).exists():
            missing.append(str(key))
    return missing


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith((".json", ".jsonl", ".md"))


def _read_many_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(_read_jsonl(path))
    return rows


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            rows.append(_decode_line(path, line_number, line))
    return rows


def _decode_line(path: Path, line_number: int, line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return _decode_error(path, line_number, exc)
    return payload if isinstance(payload, dict) else _decode_error(path, line_number, None)


def _decode_error(path: Path, line_number: int, exc: json.JSONDecodeError | None) -> dict[str, Any]:
    return {
        "version": CONTROL_PLANE_DECODE_ERROR_SCHEMA.version,
        "schema": runtime_memory_schema_payload(CONTROL_PLANE_DECODE_ERROR_SCHEMA),
        "kind": "control_plane_decode_error",
        "path": str(path),
        "line_number": line_number,
        "error": str(exc) if exc else "JSONL row is not an object",
    }


def _limited(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return records[:limit] if limit > 0 else records


__all__ = ["MemoryControlPlaneQueryOptions", "query_memory_control_plane"]
