# LLM: file_write_session_recovery contains forgiving write-session recovery helpers.
# 模块用途: 处理真实模型常见的 begin/append 顺序错误和 chunk 估算过大，不让主 service 继续变厚。

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _required_path
from .file_write_session_io import (
    append_envelope,
    failure,
    materialize_preview_if_complete,
    path_record,
    sha256_text,
    success,
    write_manifest,
)
from .file_write_session_models import (
    ChunkWriteRequest,
    FileWriteSessionPaths,
    FileWriteSessionServiceContext,
    InitialManifestRequest,
)
from .models import ToolExecutionResult


# LLM: target_from_params validates the final file path before content is trusted.
# 函数用途: 读取 target_path 并检查工作区边界；失败返回结构化工具错误。
def target_from_params(
    context: FileWriteSessionServiceContext,
    params: dict[str, Any],
) -> tuple[str, Path] | ToolExecutionResult:
    try:
        raw_target_path = _required_path(params.get("target_path"), name="target_path")
        return raw_target_path, context.resolve_path(raw_target_path)
    except ValueError as exc:
        return failure("PATH_OUTSIDE_WORKSPACE", "PATH_OUTSIDE_WORKSPACE", str(exc))


# LLM: auto_start_session creates a recoverable open session for append-first model calls.
# 函数用途: append 找不到 session 但带 target_path 时，用调用方 session_id 自动创建 open session。
def auto_start_session(
    context: FileWriteSessionServiceContext,
    params: dict[str, Any],
    *,
    session_id: str,
    paths: FileWriteSessionPaths,
) -> tuple[dict[str, Any], ToolExecutionResult | None]:
    target = target_from_params(context, params)
    if isinstance(target, ToolExecutionResult):
        return {}, target
    raw_target_path, resolved_target = target
    if paths.session_dir.exists():
        shutil.rmtree(paths.session_dir, ignore_errors=True)
    paths.chunks_dir.mkdir(parents=True, exist_ok=True)
    paths.temp_path.touch()
    manifest = initial_manifest(
        InitialManifestRequest(
            context=context,
            session_id=session_id,
            raw_path=raw_target_path,
            target=resolved_target,
            paths=paths,
            runtime_scope=_runtime_scope(params),
        )
    )
    write_manifest(paths.manifest_path, manifest)
    return manifest, None


# LLM: initial_manifest writes raw/display/resolved target facts for audit and recovery.
# 函数用途: 生成 begin 后的 manifest 初始结构。
def initial_manifest(request: InitialManifestRequest) -> dict[str, Any]:
    manifest = {
        "version": 1,
        "session_id": request.session_id,
        "status": "open",
        "target_path": {
            "raw": request.raw_path,
            "display": request.context.display_path(request.target),
            "resolved": str(request.target),
        },
        "temp_path": path_record(request.paths.temp_path, request.context.workspace_root),
        "manifest_path": path_record(request.paths.manifest_path, request.context.workspace_root),
        "chunks": {},
    }
    if request.runtime_scope:
        manifest["scope"] = dict(request.runtime_scope)
    return manifest


# LLM: _runtime_scope copies machine run ids from tool payloads into open write manifests.
# 函数用途: 让未 finish 的 file_write_session 只约束原 request，避免旧会话污染后续独立任务。
def _runtime_scope(params: dict[str, Any]) -> dict[str, str]:
    value = params.get("_runtime_scope")
    if not isinstance(value, dict):
        return {}
    scope = {
        "request_id": str(value.get("request_id") or ""),
        "run_id": str(value.get("run_id") or ""),
        "task_id": str(value.get("task_id") or ""),
    }
    return {key: item for key, item in scope.items() if item}


# LLM: write_chunk persists one validated chunk and updates manifest in memory.
# 函数用途: 将 chunk 写到 session/chunks，并登记 index/file/hash/size。
def write_chunk(request: ChunkWriteRequest) -> None:
    chunk_name = f"{request.chunk_index:08d}.chunk"
    (request.paths.chunks_dir / chunk_name).write_text(request.content, encoding="utf-8")
    request.manifest["chunks"][str(request.chunk_index)] = {
        "index": request.chunk_index,
        "file": f"chunks/{chunk_name}",
        "sha256": request.content_hash,
        "size": len(request.content),
    }


# LLM: append_split_chunks turns one large model payload into stable chunk records.
# 函数用途: 将超过单 chunk 上限的 content 自动拆分并写入连续 chunk，保留幂等冲突检查。
def append_split_chunks(
    request: ChunkWriteRequest,
    *,
    max_chunk_chars: int,
    auto_started: bool,
) -> ToolExecutionResult:
    planned = _planned_split_chunks(request.content, request.chunk_index, max_chunk_chars)
    duplicate_indexes: list[int] = []
    for chunk_index, piece, content_hash in planned:
        existing = request.manifest["chunks"].get(str(chunk_index))
        if not existing:
            continue
        if existing.get("sha256") == content_hash and existing.get("size") == len(piece):
            duplicate_indexes.append(chunk_index)
            continue
        return failure(
            "TOOL_INVALID_ARGUMENTS",
            "CHUNK_CONFLICT",
            "chunk_index already exists with different content",
            {"session_id": request.manifest["session_id"], "chunk_index": chunk_index},
        )
    for chunk_index, piece, content_hash in planned:
        if chunk_index not in duplicate_indexes:
            write_chunk(ChunkWriteRequest(request.paths, request.manifest, chunk_index, piece, content_hash))
    materialize_preview_if_complete(request.paths, request.manifest)
    write_manifest(request.paths.manifest_path, request.manifest)
    envelope = append_envelope(request.manifest, request.paths, request.chunk_index, duplicate=False)
    envelope.update(
        {
            "auto_split": True,
            "split_chunk_indexes": [item[0] for item in planned],
            "max_chunk_chars": max_chunk_chars,
        }
    )
    if auto_started:
        envelope["auto_started"] = True
    return success("append", envelope)


# LLM: _planned_split_chunks is pure so tests and service code get deterministic chunk indexes.
# 函数用途: 根据起始下标和 chunk 上限计算自动拆分后的 chunk 计划。
def _planned_split_chunks(
    content: str,
    chunk_index: int,
    max_chunk_chars: int,
) -> list[tuple[int, str, str]]:
    pieces = [
        content[index : index + max_chunk_chars]
        for index in range(0, len(content), max_chunk_chars)
    ]
    return [(chunk_index + offset, piece, sha256_text(piece)) for offset, piece in enumerate(pieces)]


__all__ = [
    "append_split_chunks",
    "auto_start_session",
    "initial_manifest",
    "target_from_params",
    "write_chunk",
]
