# LLM: File write session service owns staged chunk state and delegates manifest IO to helpers.
# 模块用途: 执行大文件 begin/append/finish/abort 分块写入，公开工具类只负责模型目录展示。

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _text_param
from .file_write_session_io import (
    append_envelope,
    existing_chunk_result,
    failure,
    materialize_preview_if_complete,
    missing_chunk_indexes,
    missing_payload,
    remove_empty_session_root,
    session_id_param,
    success,
    write_manifest,
)
from .file_write_session_models import (
    DEFAULT_MAX_SESSION_CHUNK_CHARS,
    ChunkWriteRequest,
    ExistingChunkRequest,
    FileWriteSessionServiceContext,
    InitialManifestRequest,
)
from .file_write_session_recovery import (
    append_split_chunks,
    auto_start_session,
    initial_manifest,
    target_from_params,
    write_chunk,
)
from .file_write_session_runtime import (
    append_chunk_request,
    begin_session_id,
    commit_session,
    load_open_session,
    load_or_auto_start_session,
    open_target_session_result,
    paths_for_session,
    protected_abort_result,
    resolved_target,
    reusable_begin_result,
)
from .models import ToolExecutionResult


# LLM: FileWriteSessionService performs side effects for staged large-file writes.
# 类用途: 提供 begin/append/finish/abort 操作；工具类只负责目录展示和参数入口。
class FileWriteSessionService:
    # LLM: __init__ stores the path boundary context used by every action.
    # 函数用途: 初始化 session 服务，不创建目录、不写文件。
    def __init__(self, context: FileWriteSessionServiceContext):
        self.context = context

    # LLM: execute is the action dispatcher; each branch returns structured envelopes.
    # 函数用途: 按 action 分发 begin/append/finish/abort 并转换参数错误为工具失败结果。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            action = _text_param(
                params.get("action"), name="action", max_chars=32, strip=True
            ).lower()
        except ValueError as exc:
            return failure("TOOL_INVALID_ARGUMENTS", "INVALID_ACTION", str(exc))
        if action == "begin":
            return self.begin(params)
        if action == "append":
            return self.append(params)
        if action == "finish":
            return self.finish(params)
        if action == "abort":
            return self.abort(params)
        return failure("TOOL_INVALID_ARGUMENTS", "INVALID_ACTION", f"unsupported action: {action}")

    # LLM: begin resolves the final target before any content is accepted.
    # 函数用途: 创建 session 目录、temp 文件和 manifest，并返回结构化路径记录。
    def begin(self, params: dict[str, Any]) -> ToolExecutionResult:
        target = target_from_params(self.context, params)
        if isinstance(target, ToolExecutionResult):
            return target
        raw_target_path, resolved_target = target
        session_id, id_error = begin_session_id(params)
        if id_error:
            return id_error
        paths = paths_for_session(self.context.workspace_root, session_id)
        existing = reusable_begin_result(session_id, paths, resolved_target)
        if existing:
            return existing
        target_conflict = open_target_session_result(
            self.context,
            requested_session_id=session_id,
            resolved_target=resolved_target,
        )
        if target_conflict:
            return target_conflict
        paths.chunks_dir.mkdir(parents=True, exist_ok=False)
        paths.temp_path.touch()
        manifest = initial_manifest(
            InitialManifestRequest(
                context=self.context,
                session_id=session_id,
                raw_path=raw_target_path,
                target=resolved_target,
                paths=paths,
            )
        )
        write_manifest(paths.manifest_path, manifest)
        return success("begin", begin_envelope(session_id, manifest, paths))

    # LLM: append stores chunks by index so retries and out-of-order writes are recoverable.
    # 函数用途: 校验或自动创建 session，必要时自动拆分大 content，再写入 chunk 文件并更新 manifest。
    def append(self, params: dict[str, Any]) -> ToolExecutionResult:
        session_id, paths, manifest, error, auto_started = load_or_auto_start_session(
            self.context,
            params,
        )
        if error:
            return error
        chunk = append_chunk_request(params, session_id, self.context.max_chunk_chars)
        if isinstance(chunk, ToolExecutionResult):
            return chunk
        chunk_index, content, content_hash = chunk
        if len(content) > self.context.max_chunk_chars:
            return append_split_chunks(
                ChunkWriteRequest(paths, manifest, chunk_index, content, content_hash),
                max_chunk_chars=self.context.max_chunk_chars,
                auto_started=auto_started,
            )
        existing_result = existing_chunk_result(
            ExistingChunkRequest(manifest, paths, chunk_index, content, content_hash)
        )
        if existing_result:
            return existing_result
        write_chunk(ChunkWriteRequest(paths, manifest, chunk_index, content, content_hash))
        materialize_preview_if_complete(paths, manifest)
        write_manifest(paths.manifest_path, manifest)
        envelope = append_envelope(manifest, paths, chunk_index, duplicate=False)
        if auto_started:
            envelope["auto_started"] = True
        return success("append", envelope)

    # LLM: finish is the only branch that moves staged content into the target path.
    # 函数用途: 校验 chunk 连续性，组装 temp 文件，并通过 os.replace 原子提交到目标文件。
    def finish(self, params: dict[str, Any]) -> ToolExecutionResult:
        session_id, paths, manifest, error = load_open_session(self.context, params)
        if error:
            return error
        missing = missing_chunk_indexes(manifest["chunks"])
        if missing:
            return failure(
                "TOOL_INVALID_ARGUMENTS",
                "MISSING_CHUNK",
                "missing chunk indexes",
                missing_payload(session_id, paths, missing),
            )
        target = resolved_target(self.context, manifest, session_id)
        if isinstance(target, ToolExecutionResult):
            return target
        return commit_session(session_id, paths, manifest, target)

    # LLM: abort removes staged content without touching the final target.
    # 函数用途: 删除 session 临时目录并返回 abort 状态，允许重复清理缺失 session。
    def abort(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            session_id = session_id_param(params.get("session_id"))
        except ValueError as exc:
            return failure("TOOL_INVALID_ARGUMENTS", "INVALID_SESSION_ID", str(exc))
        paths = paths_for_session(self.context.workspace_root, session_id)
        if not paths.session_dir.exists():
            return failure(
                "TOOL_INVALID_ARGUMENTS",
                "SESSION_NOT_FOUND",
                "session not found",
                {"session_id": session_id},
            )
        protected = protected_abort_result(params, session_id, paths)
        if protected:
            return protected
        shutil.rmtree(paths.session_dir, ignore_errors=True)
        remove_empty_session_root(paths.session_dir.parent)
        return success("abort", {"session_id": session_id, "status": "aborted"})

# LLM: begin_envelope keeps begin success output compact and stable.
# 函数用途: 返回 session_id、目标路径和下一 chunk 下标。
def begin_envelope(
    session_id: str,
    manifest: dict[str, Any],
    paths: FileWriteSessionPaths,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "status": "open",
        "target_path": manifest["target_path"],
        "temp_path": str(paths.temp_path),
        "manifest_path": str(paths.manifest_path),
        "next_chunk_index": next_chunk_index(manifest),
    }


# LLM: next_chunk_index derives append guidance from manifest chunk facts.
# 函数用途: 根据已接收的 chunks 返回下一个建议下标；不解析自然语言进度。
def next_chunk_index(manifest: dict[str, Any]) -> int:
    chunks = manifest.get("chunks") or {}
    if not chunks:
        return 0
    return max(int(index) for index in chunks) + 1


# LLM: received_chunk_indexes derives progress from manifest chunks.
# 函数用途: 返回已接收 chunk 下标，供重复 begin 冲突响应给模型续写。
def received_chunk_indexes(manifest: dict[str, Any]) -> list[int]:
    chunks = manifest.get("chunks") or {}
    return sorted(int(index) for index in chunks)


__all__ = [
    "DEFAULT_MAX_SESSION_CHUNK_CHARS",
    "FileWriteSessionService",
    "FileWriteSessionServiceContext",
]
