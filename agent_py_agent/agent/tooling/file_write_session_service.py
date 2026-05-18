# LLM: File write session service owns staged chunk state and delegates manifest IO to helpers.
# 模块用途: 执行大文件 begin/append/finish/abort 分块写入，公开工具类只负责模型目录展示。

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _int_param, _text_param
from .file_write_session_io import (
    append_envelope,
    chunk_param_failure,
    existing_chunk_result,
    failure,
    load_manifest,
    missing_chunk_indexes,
    missing_payload,
    remove_empty_session_root,
    session_id_param,
    sha256_text,
    success,
    write_manifest,
    write_temp_from_chunks,
)
from .file_write_session_models import (
    DEFAULT_MAX_SESSION_CHUNK_CHARS,
    SESSION_ROOT_NAME,
    ChunkWriteRequest,
    ExistingChunkRequest,
    FileWriteSessionPaths,
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
        session_id = uuid.uuid4().hex
        paths = paths_for_session(self.context.workspace_root, session_id)
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
        shutil.rmtree(paths.session_dir, ignore_errors=True)
        remove_empty_session_root(paths.session_dir.parent)
        return success("abort", {"session_id": session_id, "status": "aborted"})


# LLM: paths_for_session ensures session ids never become arbitrary filesystem paths.
# 函数用途: 根据已校验的 session_id 生成该 session 的所有内部路径。
def paths_for_session(workspace_root: Path, session_id: str) -> FileWriteSessionPaths:
    session_dir = workspace_root / SESSION_ROOT_NAME / session_id
    return FileWriteSessionPaths(
        session_dir,
        session_dir / "manifest.json",
        session_dir / "write.tmp",
        session_dir / "chunks",
    )


# LLM: load_open_session centralizes session lookup and manifest validation.
# 函数用途: 读取 open 状态的 session manifest，失败时返回结构化工具错误。
def load_open_session(
    context: FileWriteSessionServiceContext,
    params: dict[str, Any],
) -> tuple[str, FileWriteSessionPaths, dict[str, Any], ToolExecutionResult | None]:
    try:
        session_id = session_id_param(params.get("session_id"))
    except ValueError as exc:
        paths = paths_for_session(context.workspace_root, "invalid")
        return "", paths, {}, failure("TOOL_INVALID_ARGUMENTS", "INVALID_SESSION_ID", str(exc))
    paths = paths_for_session(context.workspace_root, session_id)
    if not paths.manifest_path.exists():
        return (
            session_id,
            paths,
            {},
            failure(
                "TOOL_INVALID_ARGUMENTS",
                "SESSION_NOT_FOUND",
                "session not found",
                {"session_id": session_id},
            ),
        )
    manifest, error = load_manifest(session_id, paths)
    if error:
        return session_id, paths, {}, error
    if manifest.get("status") != "open":
        return (
            session_id,
            paths,
            manifest,
            failure(
                "TOOL_INVALID_ARGUMENTS",
                "SESSION_NOT_OPEN",
                "session is not open",
                {"session_id": session_id, "status": manifest.get("status")},
            ),
        )
    return session_id, paths, manifest, None


# LLM: load_or_auto_start_session absorbs recoverable begin/append ordering mistakes from real models.
# 函数用途: append 找不到 session 但带 target_path 时，用调用方 session_id 自动创建 open session。
def load_or_auto_start_session(
    context: FileWriteSessionServiceContext,
    params: dict[str, Any],
) -> tuple[str, FileWriteSessionPaths, dict[str, Any], ToolExecutionResult | None, bool]:
    session_id, paths, manifest, error = load_open_session(context, params)
    if not error:
        return session_id, paths, manifest, None, False
    if error.result_envelope.get("code") != "SESSION_NOT_FOUND" or params.get("target_path") is None:
        return session_id, paths, manifest, error, False
    manifest, start_error = auto_start_session(context, params, session_id=session_id, paths=paths)
    if start_error:
        return session_id, paths, manifest, start_error, False
    return session_id, paths, manifest, None, True


# LLM: append_chunk_request normalizes append params and chunk identity.
# 函数用途: 读取 chunk_index/content，并返回可写入 manifest 的 hash。
def append_chunk_request(
    params: dict[str, Any],
    session_id: str,
    max_chunk_chars: int,
) -> tuple[int, str, str] | ToolExecutionResult:
    payload_limit = max(max_chunk_chars, 1_000_000)
    try:
        chunk_index = _int_param(
            params.get("chunk_index"), name="chunk_index", default=-1, min_value=0
        )
        content = _text_param(
            params.get("content"),
            name="content",
            max_chars=payload_limit,
            allow_empty=True,
        )
    except ValueError as exc:
        return chunk_param_failure(exc, session_id, max_chunk_chars)
    return chunk_index, content, sha256_text(content)


# LLM: resolved_target revalidates the stored final target before commit.
# 函数用途: 从 manifest.target_path.resolved 恢复目标路径，并再次检查工作区边界。
def resolved_target(
    context: FileWriteSessionServiceContext,
    manifest: dict[str, Any],
    session_id: str,
) -> Path | ToolExecutionResult:
    try:
        return context.resolve_path(Path(manifest["target_path"]["resolved"]))
    except ValueError as exc:
        return failure(
            "PATH_OUTSIDE_WORKSPACE",
            "PATH_OUTSIDE_WORKSPACE",
            str(exc),
            {"session_id": session_id},
        )

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
        "next_chunk_index": 0,
    }


# LLM: commit_session atomically moves staged content into the target path and cleans session state.
# 函数用途: 组装 temp 文件、创建父目录、原子替换目标文件，并删除 session 临时目录。
def commit_session(
    session_id: str,
    paths: FileWriteSessionPaths,
    manifest: dict[str, Any],
    target: Path,
) -> ToolExecutionResult:
    try:
        write_temp_from_chunks(paths, manifest)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(paths.temp_path, target)
    except OSError as exc:
        return failure("WRITE_FORBIDDEN", "COMMIT_FAILED", str(exc), {"session_id": session_id})
    envelope = {
        "session_id": session_id,
        "status": "finished",
        "target_path": manifest["target_path"],
        "chunks_committed": len(manifest["chunks"]),
    }
    shutil.rmtree(paths.session_dir, ignore_errors=True)
    remove_empty_session_root(paths.session_dir.parent)
    return success("finish", envelope)


__all__ = [
    "DEFAULT_MAX_SESSION_CHUNK_CHARS",
    "FileWriteSessionService",
    "FileWriteSessionServiceContext",
]
