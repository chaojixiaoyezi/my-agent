
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def tool_output_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read_tool_output_index(Path(workspace))
    return [_source_ref(row) for row in rows if _matches_scope(row, scope)]


def tool_output_artifact_refs(restore_refs: dict[str, Any]) -> list[dict[str, Any]]:
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs.get("source_refs"), dict) else {}
    items = source_refs.get("tool_outputs", []) if isinstance(source_refs.get("tool_outputs"), list) else []
    return [
        {
            "kind": "tool_output",
            "path": str(item.get("path", "") or ""),
            "tool": str(item.get("tool", "") or ""),
            "call_id": str(item.get("call_id", "") or ""),
            "scoped_call_id": str(item.get("scoped_call_id", "") or ""),
            "source_path": str(item.get("source_input") or ""),
            "parameters": dict(item.get("parameters", {}) if isinstance(item.get("parameters"), dict) else {}),
            "sha256": str(item.get("sha256", "") or ""),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "reserved": {},
        }
        for item in items
        if item.get("path")
    ]


def _read_tool_output_index(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "blobs" / "tool_outputs" / "index.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if payload := _json_line(line):
            rows.append(payload)
    return rows


def _matches_scope(row: dict[str, Any], scope: dict[str, Any]) -> bool:
    return all(
        not expected or str(row.get(key) or "") == str(expected)
        for key in ("request_id", "run_id", "task_id")
        if (expected := scope.get(key))
    )


def _source_ref(row: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(row.get("path") or ""))
    return {
        "kind": "tool_output",
        "path": str(path),
        "artifact_ref": str(path),
        "exists": path.exists(),
        "tool": str(row.get("tool", "") or ""),
        "call_id": str(row.get("call_id", "") or ""),
        "scoped_call_id": str(row.get("scoped_call_id", "") or ""),
        "source_input": str(row.get("source_input") or ""),
        "source_path": str(row.get("source_input") or ""),
        "parameters": dict(row.get("parameters", {}) if isinstance(row.get("parameters"), dict) else {}),
        "request_id": str(row.get("request_id", "") or ""),
        "run_id": str(row.get("run_id", "") or ""),
        "task_id": str(row.get("task_id", "") or ""),
        "sha256": str(row.get("sha256", "") or ""),
        "size_bytes": int(row.get("size_bytes", 0) or 0),
        "reserved": {},
    }


def _json_line(line: str) -> dict[str, Any]:
    if not line.strip():
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["tool_output_artifact_refs", "tool_output_source_refs"]
