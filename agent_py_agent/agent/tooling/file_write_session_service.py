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
    materialize_preview_if_complete,
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


# LLM: begin_session_id treats caller-provided session ids as stable operation keys.
# 函数用途: begin 可复用调用方传入的 session_id；未传时才生成随机 id。
def begin_session_id(params: dict[str, Any]) -> tuple[str, ToolExecutionResult | None]:
    if params.get("session_id") is None:
        return uuid.uuid4().hex, None
    try:
        return session_id_param(params.get("session_id")), None
    except ValueError as exc:
        return "", failure("TOOL_INVALID_ARGUMENTS", "INVALID_SESSION_ID", str(exc))


# LLM: reusable_begin_result makes begin idempotent for the same open target.
# 函数用途: 重复 begin 同一 session/target 时返回已有会话；同 id 不同目标则结构化失败。
def reusable_begin_result(
    session_id: str,
    paths: FileWriteSessionPaths,
    resolved_target: Path,
) -> ToolExecutionResult | None:
    if not paths.session_dir.exists():
        return None
    if not paths.manifest_path.exists():
        return failure(
            "TOOL_INVALID_ARGUMENTS",
            "SESSION_MANIFEST_MISSING",
            "session directory exists without manifest",
            {"session_id": session_id, "session_dir": str(paths.session_dir)},
        )
    manifest, error = load_manifest(session_id, paths)
    if error:
        return error
    if manifest.get("status") != "open":
        return failure(
            "TOOL_INVALID_ARGUMENTS",
            "SESSION_NOT_OPEN",
            "session is not open",
            {"session_id": session_id, "status": manifest.get("status")},
        )
    if str(resolved_target) != str(manifest.get("target_path", {}).get("resolved", "")):
        return failure(
            "TOOL_INVALID_ARGUMENTS",
            "SESSION_TARGET_CONFLICT",
            "session_id already belongs to a different target",
            {
                "session_id": session_id,
                "existing_target_path": manifest.get("target_path"),
                "requested_target_path": str(resolved_target),
            },
        )
    envelope = begin_envelope(session_id, manifest, paths)
    envelope["duplicate"] = True
    envelope["reused"] = True
    return success("begin", envelope)


# LLM: open_target_session_result prevents duplicate open sessions for the same final artifact path.
# 函数用途: begin 新 session 前扫描已有 open manifest；同目标已打开时返回推荐 session 供续写。
def open_target_session_result(
    context: FileWriteSessionServiceContext,
    *,
    requested_session_id: str,
    resolved_target: Path,
) -> ToolExecutionResult | None:
    root = context.workspace_root / SESSION_ROOT_NAME
    if not root.exists():
        return None
    for session_dir in root.iterdir():
        if not session_dir.is_dir() or session_dir.name == requested_session_id:
            continue
        paths = paths_for_session(context.workspace_root, session_dir.name)
        if not paths.manifest_path.exists():
            continue
        manifest, error = load_manifest(session_dir.name, paths)
        if error or manifest.get("status") != "open":
            continue
        if str(resolved_target) != str(manifest.get("target_path", {}).get("resolved", "")):
            continue
        return failure(
            "TOOL_INVALID_ARGUMENTS",
            "TARGET_HAS_OPEN_SESSION",
            "target already has an open file_write_session",
            {
                "requested_session_id": requested_session_id,
                "recommended_session_id": session_dir.name,
                "target_path": manifest.get("target_path") or {},
                "manifest_path": str(paths.manifest_path),
                "received_chunks": received_chunk_indexes(manifest),
                "next_chunk_index": next_chunk_index(manifest),
                "resume_action": "append_from_next_chunk_then_finish",
            },
        )
    return None


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


# LLM: protected_abort_result keeps staged progress from being discarded by an accidental abort.
# 函数用途: 非空 open session 默认拒绝 abort；调用方必须传 discard_chunks=true 才会丢弃已有 chunks。
def protected_abort_result(
    params: dict[str, Any],
    session_id: str,
    paths: FileWriteSessionPaths,
) -> ToolExecutionResult | None:
    if bool(params.get("discard_chunks")):
        return None
    if not paths.manifest_path.exists():
        return None
    manifest, error = load_manifest(session_id, paths)
    if error or manifest.get("status") != "open" or not manifest.get("chunks"):
        return None
    return failure(
        "TOOL_INVALID_ARGUMENTS",
        "SESSION_HAS_CHUNKS",
        "session has staged chunks; finish it or pass discard_chunks=true",
        {
            "session_id": session_id,
            "received_chunks": received_chunk_indexes(manifest),
            "next_chunk_index": next_chunk_index(manifest),
            "resume_action": "append_from_next_chunk_then_finish",
        },
    )


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
