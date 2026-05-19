# LLM: File-write session inspection exposes unfinished staged writes as structured runtime facts.
# 模块用途: 扫描工作区内 open 的 file_write_session manifest，供运行循环阻止未提交产物假收口。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .file_write_session_models import SESSION_ROOT_NAME


# LLM: open_file_write_sessions reads only manifests, never staged chunk bodies.
# 函数用途: 返回当前工作区未 finish/abort 的分块写入会话，用于提示模型继续 finish。
def open_file_write_sessions(workspace_root: Path, *, limit: int = 5) -> list[dict[str, Any]]:
    session_root = workspace_root / SESSION_ROOT_NAME
    if not session_root.exists():
        return []
    sessions = [_open_session_summary(path) for path in sorted(session_root.glob("*/manifest.json"))]
    return [item for item in sessions if item is not None][: max(0, limit)]


# LLM: _open_session_summary normalizes one manifest into a prompt-safe status record.
# 函数用途: 读取单个 session manifest，只保留 session_id、目标路径和 chunk 下标等小字段。
def _open_session_summary(path: Path) -> dict[str, Any] | None:
    manifest = _read_manifest(path)
    if not manifest or manifest.get("status") != "open":
        return None
    chunks = _chunk_indexes(manifest.get("chunks"))
    preview_path = _preview_path(path, manifest)
    return {
        "session_id": str(manifest.get("session_id") or path.parent.name),
        "status": "open",
        "target_path": manifest.get("target_path") or {},
        "manifest_path": str(path),
        "preview_path": str(preview_path),
        "preview_materialized": preview_path.exists() and not _missing_chunk_indexes(chunks),
        "received_chunks": chunks,
        "next_chunk_index": (max(chunks) + 1) if chunks else 0,
        "continue_tool_call": {
            "tool": "file_write_session",
            "action": "append",
            "session_id": str(manifest.get("session_id") or path.parent.name),
            "chunk_index": (max(chunks) + 1) if chunks else 0,
        },
        "finish_tool_call": {
            "tool": "file_write_session",
            "action": "finish",
            "session_id": str(manifest.get("session_id") or path.parent.name),
        },
    }


# LLM: _read_manifest keeps corrupt manifests from crashing the agent loop.
# 函数用途: 容错读取 JSON manifest；坏 manifest 留给后续 doctor/测试处理，不阻塞普通运行。
def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# LLM: _chunk_indexes treats manifest chunk keys as machine state, not text.
# 函数用途: 从 chunks 字典键提取已收到的 chunk_index。
def _chunk_indexes(value: object) -> list[int]:
    if not isinstance(value, dict):
        return []
    indexes: list[int] = []
    for key in value:
        try:
            indexes.append(int(key))
        except (TypeError, ValueError):
            continue
    return sorted(indexes)


# LLM: _missing_chunk_indexes checks only integer chunk indexes from the manifest.
# 函数用途: 判断 preview 是否由连续 chunks 组装，避免把缺块 temp 当完整状态。
def _missing_chunk_indexes(indexes: list[int]) -> list[int]:
    if not indexes:
        return []
    return [index for index in range(max(indexes) + 1) if index not in set(indexes)]


# LLM: _preview_path resolves the staged preview path without trusting prose output.
# 函数用途: 优先使用 manifest.temp_path.resolved，缺失时回退到 session 目录下 write.tmp。
def _preview_path(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    temp_path = manifest.get("temp_path") if isinstance(manifest.get("temp_path"), dict) else {}
    resolved = str(temp_path.get("resolved") or "")
    return Path(resolved) if resolved else manifest_path.parent / "write.tmp"
