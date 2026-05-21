# LLM: File write session runtime helpers keep session manifests and commit logic outside the service facade.
# 模块用途: 承载 file_write_session 的路径、会话加载、冲突检查和提交辅助函数，缩小 service 文件体积。

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _int_param, _text_param
from .file_write_session_io import (
    chunk_param_failure,
    failure,
    load_manifest,
    remove_empty_session_root,
    session_id_param,
    sha256_text,
    success,
    write_temp_from_chunks,
)
from .file_write_session_models import (
    SESSION_ROOT_NAME,
    FileWriteSessionPaths,
    FileWriteSessionServiceContext,
)
from .file_write_session_recovery import auto_start_session
from .models import ToolExecutionResult
from .structured_commit_validation import validate_structured_commit


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
    from .file_write_session_service import begin_envelope

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
        from .file_write_session_service import next_chunk_index, received_chunk_indexes

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
    from .file_write_session_service import next_chunk_index, received_chunk_indexes

    return failure(
        "TOOL_INVALID_ARGUMENTS",
        "SESSION_HAS_CHUNKS",
        "session has staged chunks; finish it or pass discard_chunks=true",
        {
            "session_id": session_id,
            "received_chunks": received_chunk_indexes(manifest),
            "next_chunk_index": next_chunk_index(manifest),
            "resume_action": "append_from_next_chunk_then_finish",
            "abort_tool_call": {
                "tool": "file_write_session",
                "action": "abort",
                "session_id": session_id,
                "discard_chunks": True,
            },
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
        validation_error = validate_structured_commit(
            session_id=session_id,
            target=target,
            temp_path=paths.temp_path,
        )
        if validation_error is not None:
            return validation_error
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
    "append_chunk_request",
    "begin_session_id",
    "commit_session",
    "load_open_session",
    "load_or_auto_start_session",
    "open_target_session_result",
    "paths_for_session",
    "protected_abort_result",
    "resolved_target",
    "reusable_begin_result",
]
