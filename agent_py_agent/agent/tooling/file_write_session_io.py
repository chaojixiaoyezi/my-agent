# LLM: file_write_session_io owns manifest persistence and stable result envelopes.
# 模块用途: 提供大文件写入 session 的路径校验、manifest IO、chunk 校验和工具结果构造。

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _text_param
from .file_write_session_models import ExistingChunkRequest, FileWriteSessionPaths
from .models import ToolExecutionResult

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


# LLM: session_id_param rejects path-like ids before filesystem path derivation.
# 函数用途: 校验 session_id 参数格式，防止调用方把它当路径传入。
def session_id_param(value: Any) -> str:
    session_id = _text_param(value, name="session_id", max_chars=80, strip=True)
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("session_id 格式无效")
    return session_id


# LLM: path_record gives manifests enough structure for audit without prose parsing.
# 函数用途: 把路径转换成 display/resolved 结构化记录。
def path_record(path: Path, workspace_root: Path) -> dict[str, str]:
    try:
        display = str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        display = str(path)
    return {"display": display, "resolved": str(path)}


# LLM: load_manifest reads JSON manifest and returns structured tool failure on corruption.
# 函数用途: 读取 session manifest，避免调用方重复写 JSON 异常处理。
def load_manifest(
    session_id: str,
    paths: FileWriteSessionPaths,
) -> tuple[dict[str, Any], ToolExecutionResult | None]:
    try:
        return json.loads(paths.manifest_path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return {}, failure(
            "TOOL_INVALID_ARGUMENTS",
            "SESSION_MANIFEST_INVALID",
            str(exc),
            {"session_id": session_id, "manifest_path": str(paths.manifest_path)},
        )


# LLM: write_manifest is the only writer for manifest.json so formatting stays stable.
# 函数用途: 原子性地刷新 session manifest 内容。
def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


# LLM: write_temp_from_chunks verifies staged files before commit.
# 函数用途: 按 chunk_index 顺序把 chunk 文件组装到 temp 文件，并校验 sha256。
def write_temp_from_chunks(paths: FileWriteSessionPaths, manifest: dict[str, Any]) -> None:
    chunks = manifest["chunks"]
    with paths.temp_path.open("w", encoding="utf-8") as temp_file:
        for index in sorted(int(item) for item in chunks):
            temp_file.write(verified_chunk_text(paths, chunks[str(index)], index))


# LLM: materialize_preview_if_complete refreshes preview only from continuous chunk facts.
# 函数用途: chunk 连续时组装 write.tmp；缺 chunk 时保留已有状态，等待后续补齐。
def materialize_preview_if_complete(
    paths: FileWriteSessionPaths,
    manifest: dict[str, Any],
) -> None:
    if missing_chunk_indexes(manifest.get("chunks") or {}):
        return
    write_temp_from_chunks(paths, manifest)


# LLM: verified_chunk_text reads and checks one staged chunk.
# 函数用途: 验证 chunk 文件 hash，防止损坏 session 被 finish。
def verified_chunk_text(paths: FileWriteSessionPaths, chunk: dict[str, Any], index: int) -> str:
    content = (paths.session_dir / chunk["file"]).read_text(encoding="utf-8")
    if sha256_text(content) != chunk["sha256"]:
        raise OSError(f"chunk checksum mismatch: {index}")
    return content


# LLM: missing_chunk_indexes makes finish validation independent from append ordering.
# 函数用途: 计算从 0 到最大已收 chunk 之间缺失的 chunk_index。
def missing_chunk_indexes(chunks: dict[str, Any]) -> list[int]:
    if not chunks:
        return []
    indexes = {int(index) for index in chunks}
    return [index for index in range(max(indexes) + 1) if index not in indexes]


# LLM: sha256_text records chunk identity for idempotency and manifest integrity.
# 函数用途: 计算文本 chunk 的 UTF-8 sha256 摘要。
def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# LLM: remove_empty_session_root cleans only the internal session container when it is empty.
# 函数用途: 在 finish/abort 后删除空的 session 根目录，保留非空状态以免误删。
def remove_empty_session_root(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return


# LLM: append_envelope keeps append success output stable for callers.
# 函数用途: 根据 manifest 生成 append 成功后的结构化结果。
def append_envelope(
    manifest: dict[str, Any],
    paths: FileWriteSessionPaths,
    chunk_index: int,
    *,
    duplicate: bool,
) -> dict[str, Any]:
    next_index = _next_chunk_index(manifest)
    preview_materialized = paths.temp_path.exists() and not missing_chunk_indexes(
        manifest["chunks"]
    )
    return {
        "session_id": manifest["session_id"],
        "status": manifest["status"],
        "target_path": manifest["target_path"],
        "manifest_path": str(paths.manifest_path),
        "temp_path": str(paths.temp_path),
        "preview_path": str(paths.temp_path),
        "preview_materialized": preview_materialized,
        "preview_char_count": _preview_char_count(paths.temp_path) if preview_materialized else 0,
        "preview_tail": _preview_tail(paths.temp_path) if preview_materialized else "",
        "chunk_index": chunk_index,
        "duplicate": duplicate,
        "received_chunks": sorted(int(index) for index in manifest["chunks"]),
        "next_chunk_index": next_index,
        "continue_tool_call": {
            "tool": "file_write_session",
            "action": "append",
            "session_id": manifest["session_id"],
            "chunk_index": next_index,
        },
        "finish_tool_call": {
            "tool": "file_write_session",
            "action": "finish",
            "session_id": manifest["session_id"],
        },
        "staging_contract": _staging_contract(),
    }


# LLM: existing_chunk_result handles append retry and conflict branches through a bundle.
# 函数用途: 相同 chunk 内容重复提交时返回幂等成功，不同内容同 index 返回冲突。
def existing_chunk_result(request: ExistingChunkRequest) -> ToolExecutionResult | None:
    existing = request.manifest["chunks"].get(str(request.chunk_index))
    if not existing:
        return None
    if existing.get("sha256") == request.content_hash and existing.get("size") == len(
        request.content
    ):
        return success(
            "append",
            append_envelope(
                request.manifest,
                request.paths,
                request.chunk_index,
                duplicate=True,
            ),
        )
    return failure(
        "TOOL_INVALID_ARGUMENTS",
        "CHUNK_CONFLICT",
        "chunk_index already exists with different content",
        {"session_id": request.manifest["session_id"], "chunk_index": request.chunk_index},
    )


# LLM: chunk_param_failure converts chunk parse errors into stable error envelopes.
# 函数用途: 区分 CHUNK_TOO_LARGE 和其他 chunk 参数错误。
def chunk_param_failure(
    exc: ValueError,
    session_id: str,
    max_chunk_chars: int,
) -> ToolExecutionResult:
    if "过长" in str(exc):
        return failure(
            "TOOL_INVALID_ARGUMENTS",
            "CHUNK_TOO_LARGE",
            str(exc),
            {"session_id": session_id, "max_chunk_chars": max_chunk_chars},
        )
    return failure("TOOL_INVALID_ARGUMENTS", "INVALID_CHUNK", str(exc), {"session_id": session_id})


# LLM: missing_payload keeps MISSING_CHUNK error details stable.
# 函数用途: 构造缺 chunk 的结构化错误字段。
def missing_payload(
    session_id: str,
    paths: FileWriteSessionPaths,
    missing: list[int],
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "missing_chunk_indexes": missing,
        "manifest_path": str(paths.manifest_path),
    }


# LLM: success gives every successful action a machine-readable envelope and JSON output.
# 函数用途: 构造 file_write_session 成功结果。
def success(action: str, envelope: dict[str, Any]) -> ToolExecutionResult:
    result_envelope = {"ok": True, "action": action, **envelope}
    return ToolExecutionResult(
        "file_write_session",
        True,
        json.dumps(result_envelope, ensure_ascii=False, sort_keys=True),
        result_envelope=result_envelope,
    )


# LLM: _next_chunk_index derives append guidance from manifest chunk facts.
# 函数用途: 根据已接收 chunks 返回下一个建议下标；不解析任何文本。
def _next_chunk_index(manifest: dict[str, Any]) -> int:
    chunks = manifest.get("chunks") or {}
    if not chunks:
        return 0
    return max(int(index) for index in chunks) + 1


# LLM: _staging_contract makes chunk and preview state authoritative for recovery.
# 函数用途: 明确 file_write_session 的事实来源、预览落盘时机和提交动作。
def _staging_contract() -> dict[str, Any]:
    return {
        "fact_source": "preview_and_chunks",
        "commit_action": "finish",
        "preview_materialized_after_append": True,
        "temp_path_materialized_on_finish": False,
    }


# LLM: _preview_char_count tells the model roughly how much staged content already exists.
# 函数用途: 读取预览文件字符数，帮助续写时估计当前位置，不需要把全量正文塞回上下文。
def _preview_char_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


# LLM: _preview_tail gives append callers a bounded continuation anchor from the staged preview.
# 函数用途: 返回已组装预览文件末尾少量文本，让模型从正确位置续写而不是重复或乱接。
def _preview_tail(path: Path, *, max_chars: int = 400) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


# LLM: failure separates stable machine codes from human-readable diagnostic text.
# 函数用途: 构造带标准 error_code 和自定义 envelope code 的失败结果。
def failure(
    error_code: str,
    code: str,
    message: str,
    envelope: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    result_envelope = {"ok": False, "code": code, "message": message, **(envelope or {})}
    return ToolExecutionResult(
        "file_write_session",
        False,
        json.dumps(result_envelope, ensure_ascii=False, sort_keys=True),
        result_envelope=result_envelope,
        error_code=error_code,
    )
