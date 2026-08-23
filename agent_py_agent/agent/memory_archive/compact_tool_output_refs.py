# LLM: 本模块读取 owner 私有的工具输出索引，为 Compact 恢复和同一 active turn 的耐久续接提供结构化记录；不得从工具正文猜状态。
# 模块用途: 从任务工作区的工具索引中恢复调用引用、输出引用和可续接的调用事实。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .tool_output_externalizer import tool_output_index_paths_for_lookup

_INTERNAL_LEDGER_TOOLS = {"task_progress"}


# LLM: Child lifecycle wakes are another slice of the same active root turn. Rehydrate only
# exact run/task index rows, preserve append order, and never scan child workspaces or prose.
# 函数用途: 从一个任务自己的工具索引恢复跨后台工作片所需的调用历史。
def carried_tool_call_records(
    workspace: str | Path,
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = _read_tool_output_index(Path(workspace))
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not _is_indexed_call_fact(row) or not _matches_scope(row, scope):
            continue
        record = _carried_tool_call_record(row)
        identity = str(record.get("scoped_call_id") or record.get("call_id") or "").strip()
        if not identity or identity in seen:
            continue
        seen.add(identity)
        records.append(record)
    return records


def tool_output_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    # 大输出恢复产物经 _write_output_artifact→_append_index 已写 kind=tool_output
    # 行(带 path)进 index.jsonl, 直接读 index 即可, 无需另扫 artifact 文件。
    rows = _read_tool_output_index(Path(workspace))
    return [_source_ref(row) for row in rows if _is_tool_output_row(row) and _matches_scope(row, scope)]


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


# LLM: A small output is indexed as tool_call and an externalized output as tool_output;
# both are one exact invocation, distinguished by scoped_call_id rather than filename.
# 函数用途: 判断索引行能否作为一次历史工具调用恢复。
def _is_indexed_call_fact(row: dict[str, Any]) -> bool:
    return (
        str(row.get("kind") or "") in {"tool_call", "tool_output"}
        and bool(str(row.get("tool") or "").strip())
        and bool(str(row.get("call_id") or "").strip())
    )


# LLM: Convert index metadata into the existing carried archive contract. Successful indexed
# calls prove the handler ran; failed legacy rows remain fail-closed because the index does not
# claim a handler boundary. Artifact paths are refs, never eagerly loaded into the prompt.
# 函数用途: 把一条工具索引转换成工具循环可重建去重、参数和结果引用的轻量记录。
def _carried_tool_call_record(row: dict[str, Any]) -> dict[str, Any]:
    path = str(row.get("path") or "").strip()
    digest = str(row.get("sha256") or "").strip()
    execution = row.get("tool_execution")
    execution = execution if isinstance(execution, dict) else {}
    handler_executed = execution.get("handler_executed")
    if not isinstance(handler_executed, bool):
        handler_executed = row.get("ok") is True
    record: dict[str, Any] = {
        "id": str(row.get("call_id") or ""),
        "call_id": str(row.get("call_id") or ""),
        "scoped_call_id": str(row.get("scoped_call_id") or ""),
        "request_id": str(row.get("request_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "tool": str(row.get("tool") or ""),
        "parameters": dict(row.get("parameters") or {})
        if isinstance(row.get("parameters"), dict)
        else {},
        "ok": row.get("ok") is True,
        "status": str(row.get("status") or ""),
        "error_code": str(row.get("error_code") or ""),
        "reported_error_code": str(row.get("reported_error_code") or ""),
        "handler_executed": handler_executed,
        "duration_ms": int(execution.get("duration_ms") or 0),
        "output_externalized": bool(row.get("output_externalized") or path),
        "output_size_bytes": int(row.get("size_bytes") or 0),
        "tool_output_trust": str(row.get("tool_output_trust") or "runtime"),
        "tool_output_redaction": str(row.get("tool_output_redaction") or "default"),
    }
    if digest:
        record["output_hash"] = digest
    failure_stage = str(execution.get("failure_stage") or "").strip()
    if failure_stage:
        record["failure_stage"] = failure_stage
    if path:
        record["output_path"] = path
        record["artifact_ref"] = path
    for key in ("read_window", "page_window"):
        value = row.get(key)
        if isinstance(value, dict):
            record[key] = dict(value)
    return record


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


__all__ = [
    "carried_tool_call_records",
    "tool_call_refs",
    "tool_call_source_refs",
    "tool_output_artifact_refs",
    "tool_output_source_refs",
]
