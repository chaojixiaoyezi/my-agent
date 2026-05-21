# LLM: File-write session inspection exposes unfinished staged writes as structured runtime facts.
# 模块用途: 扫描工作区内 open 的 file_write_session manifest，供运行循环阻止未提交产物假收口。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .file_write_session_models import SESSION_ROOT_NAME


# LLM: open_file_write_sessions reads only manifests, never staged chunk bodies.
# 函数用途: 返回当前工作区未 finish/abort 的分块写入会话，用于提示模型继续 finish。
def open_file_write_sessions(
    workspace_root: Path,
    *,
    limit: int = 5,
    scope: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    session_root = workspace_root / SESSION_ROOT_NAME
    if not session_root.exists():
        return []
    sessions = [_open_session_summary(path) for path in sorted(session_root.glob("*/manifest.json"))]
    filtered = [
        item
        for item in sessions
        if item is not None and _scope_matches(item, scope=scope)
    ]
    return filtered[: max(0, limit)]


# LLM: _open_session_summary normalizes one manifest into a prompt-safe status record.
# 函数用途: 读取单个 session manifest，只保留 session_id、目标路径和 chunk 下标等小字段。
def _open_session_summary(path: Path) -> dict[str, Any] | None:
    manifest = _read_manifest(path)
    if not manifest or manifest.get("status") != "open":
        return None
    chunks = _chunk_indexes(manifest.get("chunks"))
    preview_path = _preview_path(path, manifest)
    preview_materialized = preview_path.exists() and not _missing_chunk_indexes(chunks)
    return {
        "session_id": str(manifest.get("session_id") or path.parent.name),
        "status": "open",
        "target_path": manifest.get("target_path") or {},
        "manifest_path": str(path),
        "preview_path": str(preview_path),
        "preview_materialized": preview_materialized,
        "preview_char_count": _preview_char_count(preview_path) if preview_materialized else 0,
        "preview_tail": _preview_tail(preview_path) if preview_materialized else "",
        "scope": _manifest_scope(manifest),
        "received_chunks": chunks,
        "next_chunk_index": (max(chunks) + 1) if chunks else 0,
        "last_finish_error": _last_finish_error(manifest),
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
        "reset_tool_call": {
            "tool": "file_write_session",
            "action": "reset",
            "session_id": str(manifest.get("session_id") or path.parent.name),
            "discard_chunks": True,
        },
        "abort_requires_discard_chunks": bool(chunks),
        "abort_tool_call": {
            "tool": "file_write_session",
            "action": "abort",
            "session_id": str(manifest.get("session_id") or path.parent.name),
            "discard_chunks": True,
        },
    }


# LLM: _last_finish_error exposes only bounded structured failure facts from manifest state.
# 函数用途: 返回最近一次 finish 失败的错误码、完整性 codes 和修复工具调用，不读取 staged 正文。
def _last_finish_error(manifest: dict[str, Any]) -> dict[str, Any]:
    value = manifest.get("last_finish_error")
    return value if isinstance(value, dict) else {}


# LLM: _scope_matches keeps write-session repairs bound to the current machine run scope.
# 函数用途: 如果调用方给了 request/run/task id，只返回同 scope 的 open session；旧无 scope 会话不污染新任务。
def _scope_matches(item: dict[str, Any], *, scope: dict[str, str] | None) -> bool:
    filters = {
        "request_id": str((scope or {}).get("request_id") or ""),
        "run_id": str((scope or {}).get("run_id") or ""),
        "task_id": str((scope or {}).get("task_id") or ""),
    }
    active = {key: value for key, value in filters.items() if value}
    if not active:
        return True
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    return any(str(scope.get(key) or "") == value for key, value in active.items())


# LLM: _manifest_scope copies only machine ids needed for run-scoped open-session repair.
# 函数用途: 从 manifest.scope 取 request_id/run_id/task_id；不读取目标文件内容或提示词。
def _manifest_scope(manifest: dict[str, Any]) -> dict[str, str]:
    value = manifest.get("scope") if isinstance(manifest.get("scope"), dict) else {}
    return {
        key: str(value.get(key) or "")
        for key in ("request_id", "run_id", "task_id")
        if str(value.get(key) or "")
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


# LLM: _preview_char_count keeps open-session summaries bounded while still exposing staged size.
# 函数用途: 返回预览文件字符数，帮助模型知道已写入的大致长度。
def _preview_char_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


# LLM: _preview_tail gives continuation context from staged content without loading the whole artifact.
# 函数用途: 返回预览尾部少量文本，供恢复/续写时对齐当前位置。
def _preview_tail(path: Path, *, max_chars: int = 400) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
