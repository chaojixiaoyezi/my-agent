# LLM: Compact tool-output refs connect externalized tool artifacts to compact/resume without reading bodies.
# 模块用途: 从 tool_outputs/index.jsonl 读取同 scope 的大工具输出引用，供 compact apply 和 work_state 使用。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: tool_output_source_refs returns scoped artifact refs from the lightweight tool-output index only.
# 函数用途: 按 request/run/task 过滤 tool output index，返回可恢复引用，不读取 artifact 正文。
def tool_output_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read_tool_output_index(Path(workspace))
    return [_source_ref(row) for row in rows if _matches_scope(row, scope)]


# LLM: tool_output_artifact_refs converts restore refs into work_state artifact refs.
# 函数用途: 将 restore_refs.source_refs.tool_outputs 转成 work_state_snapshot.artifact_refs，供自检和恢复显示使用。
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
            "sha256": str(item.get("sha256", "") or ""),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "reserved": {},
        }
        for item in items
        if item.get("path")
    ]


# LLM: _read_tool_output_index tolerates missing/corrupt index rows and never opens artifact bodies.
# 函数用途: 读取 memory_archive/artifacts/tool_outputs/index.jsonl 中的轻量索引记录。
def _read_tool_output_index(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "memory_archive" / "artifacts" / "tool_outputs" / "index.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if payload := _json_line(line):
            rows.append(payload)
    return rows


# LLM: _matches_scope keeps compact apply from importing unrelated tool output artifacts.
# 函数用途: 对 request_id/run_id/task_id 做空值通配匹配，确保同一任务范围内的 artifact 才进入恢复包。
def _matches_scope(row: dict[str, Any], scope: dict[str, Any]) -> bool:
    return all(
        not expected or str(row.get(key) or "") == str(expected)
        for key in ("request_id", "run_id", "task_id")
        if (expected := scope.get(key))
    )


# LLM: _source_ref normalizes the public restore-ref shape for one tool-output index row.
# 函数用途: 生成 tool output 恢复引用条目，保留路径、hash、scope 和 call id。
def _source_ref(row: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(row.get("path") or ""))
    return {
        "kind": "tool_output",
        "path": str(path),
        "exists": path.exists(),
        "tool": str(row.get("tool", "") or ""),
        "call_id": str(row.get("call_id", "") or ""),
        "scoped_call_id": str(row.get("scoped_call_id", "") or ""),
        "request_id": str(row.get("request_id", "") or ""),
        "run_id": str(row.get("run_id", "") or ""),
        "task_id": str(row.get("task_id", "") or ""),
        "sha256": str(row.get("sha256", "") or ""),
        "size_bytes": int(row.get("size_bytes", 0) or 0),
        "reserved": {},
    }


# LLM: _json_line keeps malformed tool-output index rows from breaking compact apply.
# 函数用途: 解析 JSONL 单行；空行、坏行或非对象行返回空 dict。
def _json_line(line: str) -> dict[str, Any]:
    if not line.strip():
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["tool_output_artifact_refs", "tool_output_source_refs"]
