
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .tool_output_externalizer import (
    tool_output_index_paths_for_lookup,
    tool_output_root,
)

_INTERNAL_LEDGER_TOOLS = {"task_progress"}


def tool_output_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read_tool_output_index(Path(workspace))
    # 大输出恢复产物以独立 artifact json 存在(kind=tool_output 载荷)——
    # index.jsonl 只记 tool_call 行; 不扫 artifact 文件则 context bundle
    # 产物更新发现不了它们(no_matching_tool_output_artifacts, 合同测试实锤)。
    rows.extend(_read_tool_output_artifact_rows(Path(workspace)))
    return [_source_ref(row) for row in rows if _is_tool_output_row(row) and _matches_scope(row, scope)]


def _read_tool_output_artifact_rows(workspace: Path) -> list[dict[str, Any]]:
    root = tool_output_root(workspace)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 坏文件跳过(审计仍可查原文件)
            continue
        if not isinstance(payload, dict) or str(payload.get("kind") or "") != "tool_output":
            continue
        row = dict(payload)
        row["path"] = str(path)
        rows.append(row)
    return rows


def tool_call_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read_tool_output_index(Path(workspace))
    return [_tool_call_ref(row) for row in rows if _is_tool_call_row(row) and _matches_scope(row, scope)]


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
            "ok": item.get("ok"),
            "status": str(item.get("status") or ""),
            "error_code": str(item.get("error_code") or ""),
            "sha256": str(item.get("sha256", "") or ""),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "read_window": dict(item.get("read_window", {}) if isinstance(item.get("read_window"), dict) else {}),
            "page_window": dict(item.get("page_window", {}) if isinstance(item.get("page_window"), dict) else {}),
        }
        for item in items
        if item.get("path") and _is_model_visible_tool_output(item)
    ]


def tool_call_refs(restore_refs: dict[str, Any]) -> list[dict[str, Any]]:
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs.get("source_refs"), dict) else {}
    items = source_refs.get("tool_calls", []) if isinstance(source_refs.get("tool_calls"), list) else []
    return [dict(item) for item in items if isinstance(item, dict)]


def _read_tool_output_index(workspace: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in tool_output_index_paths_for_lookup(workspace):
        if not path.exists():
            continue
        rows.extend(_read_tool_output_index_path(path))
    return rows


def _read_tool_output_index_path(path: Path) -> list[dict[str, Any]]:
    return [
        payload
        for line in path.read_text(encoding="utf-8").splitlines()
        if (payload := _json_line(line))
    ]


def _matches_scope(row: dict[str, Any], scope: dict[str, Any]) -> bool:
    return all(
        not expected or str(row.get(key) or "") == str(expected)
        for key in ("request_id", "run_id", "task_id")
        if (expected := scope.get(key))
    )


def _is_tool_output_row(row: dict[str, Any]) -> bool:
    return (
        str(row.get("kind") or "") == "tool_output"
        and bool(str(row.get("path") or "").strip())
        and _is_model_visible_tool_output(row)
    )


def _is_model_visible_tool_output(row: dict[str, Any]) -> bool:
    return str(row.get("tool") or "").strip() not in _INTERNAL_LEDGER_TOOLS


def _is_tool_call_row(row: dict[str, Any]) -> bool:
    return str(row.get("kind") or "") == "tool_call"


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
        "ok": row.get("ok"),
        "status": str(row.get("status") or ""),
        "error_code": str(row.get("error_code") or ""),
        "sha256": str(row.get("sha256", "") or ""),
        "size_bytes": int(row.get("size_bytes", 0) or 0),
        "read_window": dict(row.get("read_window", {}) if isinstance(row.get("read_window"), dict) else {}),
        "page_window": dict(row.get("page_window", {}) if isinstance(row.get("page_window"), dict) else {}),
    }


def _tool_call_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "tool_call",
        "tool": str(row.get("tool", "") or ""),
        "call_id": str(row.get("call_id", "") or ""),
        "scoped_call_id": str(row.get("scoped_call_id", "") or ""),
        "source_input": str(row.get("source_input") or ""),
        "source_path": str(row.get("source_input") or ""),
        "parameters": dict(row.get("parameters", {}) if isinstance(row.get("parameters"), dict) else {}),
        "request_id": str(row.get("request_id", "") or ""),
        "run_id": str(row.get("run_id", "") or ""),
        "task_id": str(row.get("task_id", "") or ""),
        "ok": row.get("ok"),
        "status": str(row.get("status") or ""),
        "error_code": str(row.get("error_code") or ""),
        "sha256": str(row.get("sha256", "") or ""),
        "size_bytes": int(row.get("size_bytes", 0) or 0),
        "output_externalized": bool(row.get("output_externalized")),
        "read_window": dict(row.get("read_window", {}) if isinstance(row.get("read_window"), dict) else {}),
        "page_window": dict(row.get("page_window", {}) if isinstance(row.get("page_window"), dict) else {}),
    }


def _json_line(line: str) -> dict[str, Any]:
    if not line.strip():
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["tool_call_refs", "tool_call_source_refs", "tool_output_artifact_refs", "tool_output_source_refs"]
