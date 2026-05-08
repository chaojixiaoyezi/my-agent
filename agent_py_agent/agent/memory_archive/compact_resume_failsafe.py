# LLM: Compact resume fail-safe helpers expose tool-output checkpoints as refs-only recovery anchors.
# 模块用途: 从 compact restore refs 的 hook JSONL 中提取工具输出外置前 checkpoint，不读取大 artifact 正文。
from __future__ import annotations

"""refs-only fail-safe checkpoint extraction for compact resume."""

import json
from pathlib import Path
from typing import Any


# LLM: collect_fail_safe_checkpoints extracts tool-output recovery anchors from metadata-only hook snapshots.
# 函数用途: 从 compact restore refs 指向的 hook JSONL 中找出工具输出外置前 checkpoint，不读取 artifact 正文。
def collect_fail_safe_checkpoints(restore_refs: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs, dict) else {}
    archive_files = source_refs.get("archive_files", []) if isinstance(source_refs, dict) else []
    for item in archive_files if isinstance(archive_files, list) else []:
        path = Path(str(item.get("path", "") or ""))
        if path.name.endswith(".jsonl"):
            checkpoints.extend(_checkpoint_lines_from_path(path))
    return _dedupe_checkpoints(checkpoints)


# LLM: _checkpoint_lines_from_path scans JSONL metadata while preserving line numbers as refs.
# 函数用途: 读取 hook 文件中的小型 checkpoint 元数据，并记录行号方便后续显式核查。
def _checkpoint_lines_from_path(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    checkpoints: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and _is_tool_output_fail_safe(payload):
            checkpoints.append(_checkpoint_payload(path, line_no, payload))
    return checkpoints


# LLM: _is_tool_output_fail_safe recognizes checkpoints written before large tool output externalization.
# 函数用途: 只接受 tool_output_externalizer 来源的 checkpoint，避免普通 hook 快照混入恢复入口。
def _is_tool_output_fail_safe(payload: dict[str, Any]) -> bool:
    turn_range = payload.get("turn_range", {}) if isinstance(payload.get("turn_range"), dict) else {}
    source = str(payload.get("source") or turn_range.get("source") or "")
    return source == "tool_output_externalizer" and isinstance(payload.get("tool_calls"), list)


# LLM: _checkpoint_payload returns refs and output identity without copying large output bodies.
# 函数用途: 生成 memory-resume 可展示的 fail-safe checkpoint 摘要，保留 path、line、hash、size 和下一步建议。
def _checkpoint_payload(path: Path, line_no: int, payload: dict[str, Any]) -> dict[str, Any]:
    turn_range = payload.get("turn_range", {}) if isinstance(payload.get("turn_range"), dict) else {}
    return {
        "path": str(path),
        "line_no": line_no,
        "snapshot_id": str(payload.get("snapshot_id", "") or ""),
        "source": str(payload.get("source") or turn_range.get("source") or ""),
        "status": str(payload.get("status", "") or ""),
        "request_id": str(payload.get("request_id") or turn_range.get("request_id") or ""),
        "run_id": str(payload.get("run_id") or turn_range.get("run_id") or ""),
        "task_id": str(payload.get("task_id") or turn_range.get("task_id") or ""),
        "tool_calls": _tool_output_refs(payload.get("tool_calls")),
        "next_actions": list(payload.get("next_actions", [])) if isinstance(payload.get("next_actions"), list) else [],
        "reads_artifact_bodies": False,
    }


# LLM: _tool_output_refs strips tool-call records to identity, hash, and size.
# 函数用途: 为 fail-safe checkpoint 保留可校验摘要，不保留 output/content/result 正文。
def _tool_output_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            refs.append({
                "tool": str(item.get("tool") or item.get("name") or ""),
                "id": str(item.get("id") or item.get("tool_call_id") or ""),
                "ok": item.get("ok"),
                "output_hash": str(item.get("output_hash", "") or ""),
                "output_size_bytes": int(item.get("output_size_bytes", 0) or 0),
                "output_externalized": str(item.get("output_externalized", "") or ""),
            })
    return refs


# LLM: _dedupe_checkpoints preserves first-seen checkpoint order from restore refs.
# 函数用途: 避免同一个 hook 文件被多个 compact 源引用重复展示。
def _dedupe_checkpoints(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        key = (str(item.get("path", "")), int(item.get("line_no", 0) or 0))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
