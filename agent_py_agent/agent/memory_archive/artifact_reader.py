# LLM: Explicit artifact reads must go through the artifact index, not arbitrary file paths.
# 模块用途: 读取已登记 tool-output artifact 的正文切片，并校验 index、路径边界和 sha256。
from __future__ import annotations

"""explicit refs-only-to-body reader for externalized tool output artifacts."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_read_modes import (
    ArtifactContentReadRequest,
    ArtifactContentReadResult,
    read_artifact_content_by_mode,
)

DEFAULT_ARTIFACT_READ_CHARS = 4000


# LLM: ReadToolOutputArtifactRequest keeps future read policy fields on one stable bundle.
# 类用途: 保存 artifact 显式读取的工作区、引用、偏移和读取长度；max_chars=0 表示读取全部。
@dataclass(frozen=True)
class ReadToolOutputArtifactRequest:
    root: str | Path
    artifact_ref: str
    offset: int = 0
    max_chars: int = DEFAULT_ARTIFACT_READ_CHARS
    mode: str = "slice"
    query: str = ""
    run_id: str = ""
    task_id: str = ""
    request_id: str = ""


# LLM: _RegisteredArtifactRead keeps internal artifact read arguments bundled for code-size guardrails.
# 类用途: 保存已通过 index 和路径边界检查的 artifact 文件、登记记录和原始读取请求。
@dataclass(frozen=True)
class _RegisteredArtifactRead:
    path: Path
    record: dict[str, Any]
    request: ReadToolOutputArtifactRequest


# LLM: read_tool_output_artifact is the only public body-read entrypoint for tool-output artifacts.
# 函数用途: 先从 tool_outputs/index.jsonl 找登记记录，再读取正文并校验 hash；不会把任意文件路径当 artifact。
def read_tool_output_artifact(request: ReadToolOutputArtifactRequest) -> dict[str, Any]:
    root = Path(request.root).expanduser().resolve(strict=False)
    artifact_ref = str(request.artifact_ref or "").strip()
    if not artifact_ref:
        return _error_payload("missing_artifact_ref", artifact_ref, "artifact_ref is required")
    record = _find_index_record(root, artifact_ref, request)
    if record is None:
        return _error_payload("artifact_not_registered", artifact_ref, "artifact ref was not found in tool output index")
    path = Path(str(record.get("path", "") or "")).expanduser().resolve(strict=False)
    allowed_root = _tool_output_root(root)
    if not _is_under_allowed_root(path, allowed_root):
        return _error_payload("artifact_path_outside_tool_outputs", artifact_ref, "registered path is outside tool_outputs")
    if not path.is_file():
        return _error_payload("artifact_missing", artifact_ref, "registered artifact file does not exist")
    return _read_registered_artifact(_RegisteredArtifactRead(path=path, record=record, request=request))


# LLM: estimate_tool_output_artifact_size reads only the lightweight index, never the artifact body.
# 函数用途: 在 read_artifact 真正展开正文前估算 artifact 大小，用于预算和安全预判。
def estimate_tool_output_artifact_size(request: ReadToolOutputArtifactRequest) -> int | None:
    root = Path(request.root).expanduser().resolve(strict=False)
    artifact_ref = str(request.artifact_ref or "").strip()
    if not artifact_ref:
        return None
    record = _find_index_record(root, artifact_ref, request)
    if record is None:
        return None
    try:
        return max(0, int(record.get("size_bytes") or 0))
    except (TypeError, ValueError):
        return None


# LLM: _read_registered_artifact validates artifact JSON and returns only the requested content slice.
# 函数用途: 读取 artifact JSON 正文，校验 kind/content/hash，再按 offset/max_chars 返回显式读取片段。
def _read_registered_artifact(read: _RegisteredArtifactRead) -> dict[str, Any]:
    path = read.path
    record = read.record
    request = read.request
    artifact_ref = request.artifact_ref
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _error_payload("artifact_unreadable", artifact_ref, f"{type(exc).__name__}: {exc}")
    content = payload.get("content")
    if payload.get("kind") != "tool_output" or not isinstance(content, str):
        return _error_payload("invalid_tool_output_artifact", artifact_ref, "artifact is not a tool_output content file")
    digest = _sha256_text(content)
    expected = str(payload.get("sha256") or record.get("sha256") or "")
    if expected and digest != expected:
        return _error_payload("artifact_hash_mismatch", artifact_ref, "artifact content hash does not match metadata")
    read_result = read_artifact_content_by_mode(
        ArtifactContentReadRequest(
            content=content,
            mode=request.mode,
            offset=request.offset,
            max_chars=request.max_chars,
            query=request.query,
        )
    )
    if not read_result.ok:
        return _error_payload(read_result.error_code, artifact_ref, read_result.message)
    base = _success_base_payload(read, payload, content, digest)
    base.update(_success_content_payload(read_result))
    base.update(read_result.metadata or {})
    return base


# LLM: _success_base_payload keeps artifact metadata assembly out of the read/validate function.
# 函数用途: 生成成功读取时的稳定 metadata 字段，不包含具体正文片段字段。
def _success_base_payload(
    read: _RegisteredArtifactRead,
    payload: dict[str, Any],
    content: str,
    digest: str,
) -> dict[str, Any]:
    record = read.record
    artifact_ref = read.request.artifact_ref
    return {
        "ok": True,
        "artifact_ref": artifact_ref,
        "artifact_path": str(read.path),
        "kind": "tool_output",
        "tool": str(payload.get("tool") or record.get("tool") or ""),
        "call_id": str(payload.get("call_id") or record.get("call_id") or ""),
        "request_id": str(payload.get("request_id") or record.get("request_id") or ""),
        "run_id": str(payload.get("run_id") or record.get("run_id") or ""),
        "task_id": str(payload.get("task_id") or record.get("task_id") or ""),
        "sha256": digest,
        "size_bytes": len(content.encode("utf-8")),
        "content_hash_verified": True,
        "reads_artifact_body": True,
    }


# LLM: _success_content_payload keeps mode-specific content fields in one small mapper.
# 函数用途: 把 ArtifactContentReadResult 转成公开 read_artifact payload 的正文相关字段。
def _success_content_payload(read_result: ArtifactContentReadResult) -> dict[str, Any]:
    return {
        "read_mode": read_result.mode,
        "content_offset": read_result.offset,
        "content_max_chars": read_result.max_chars,
        "content_chars": len(read_result.content),
        "truncated": read_result.truncated,
        "content": read_result.content,
    }

# LLM: _find_index_record accepts registered path/hash/call id refs while keeping index as authority.
# 函数用途: 从 tool output index 中查找用户传入的 artifact ref；找不到就拒绝读取。
def _find_index_record(
    root: Path,
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
) -> dict[str, Any] | None:
    index_path = _tool_output_root(root) / "index.jsonl"
    records = _index_records(index_path)
    ref_path = Path(artifact_ref).expanduser()
    resolved_ref = ref_path.resolve(strict=False) if ref_path.is_absolute() or _looks_like_path(artifact_ref) else None
    matches = [
        record
        for record in records
        if _record_matches_ref(record, artifact_ref, resolved_ref)
    ]
    scoped = _scoped_matches(matches, artifact_ref, request)
    if scoped:
        return scoped[-1]
    if matches:
        return matches[-1]
    if resolved_ref is not None:
        return _unique_record_by_basename(records, ref_path.name)
    return None


# LLM: _record_matches_ref treats path/hash/scoped-call-id/call-id as index keys.
# 函数用途: 判断 index 行是否命中模型传入的 artifact_ref，避免读取未登记文件。
def _record_matches_ref(
    record: dict[str, Any],
    artifact_ref: str,
    resolved_ref: Path | None,
) -> bool:
    record_path = Path(str(record.get("path", "") or "")).expanduser().resolve(strict=False)
    literal_refs = {
        str(record.get("path", "") or ""),
        str(record.get("sha256", "") or ""),
        str(record.get("scoped_call_id", "") or ""),
        str(record.get("call_id", "") or ""),
    }
    return artifact_ref in literal_refs or bool(resolved_ref is not None and record_path == resolved_ref)


# LLM: _scoped_matches prefers the current run/task/request when short refs collide.
# 函数用途: 对 `17-1` 这类短 call_id，优先选同一 run/task/request 的记录；没作用域时保持最新记录优先。
def _scoped_matches(
    matches: list[dict[str, Any]],
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
) -> list[dict[str, Any]]:
    if ":" in artifact_ref:
        return matches
    exact = [record for record in matches if _record_scope_matches_request(record, request)]
    if exact:
        return exact
    return [record for record in matches if _record_has_scope(record)]


# LLM: _record_scope_matches_request checks current runner scope without trusting model text.
# 函数用途: 让 read_artifact("17-1") 在 runner 内优先命中当前 run/task/request 的同号工具输出。
def _record_scope_matches_request(
    record: dict[str, Any],
    request: ReadToolOutputArtifactRequest,
) -> bool:
    return any(
        str(getattr(request, key) or "").strip()
        and str(record.get(key) or "").strip() == str(getattr(request, key) or "").strip()
        for key in ("run_id", "task_id", "request_id")
    )


# LLM: _record_has_scope lets scoped records outrank legacy unscoped records.
# 函数用途: 判断 artifact index 行是否携带 run/task/request 标识，防止读到旧 run 的同号 call_id。
def _record_has_scope(record: dict[str, Any]) -> bool:
    return any(str(record.get(key) or "").strip() for key in ("run_id", "task_id", "request_id"))


# LLM: _unique_record_by_basename repairs copied artifact paths with a wrong workspace prefix only.
# 函数用途: 当模型把已登记 artifact 的目录前缀抄错时，用唯一文件名匹配回 index 记录。
def _unique_record_by_basename(records: list[dict[str, Any]], filename: str) -> dict[str, Any] | None:
    if not filename.endswith(".json"):
        return None
    matches = [
        record
        for record in records
        if Path(str(record.get("path", "") or "")).name == filename
    ]
    return matches[0] if len(matches) == 1 else None


# LLM: _index_records is tolerant of corrupt lines but never treats index absence as permission to read files.
# 函数用途: 读取 tool_outputs/index.jsonl 的有效 JSON 对象行；坏行跳过，缺失则返回空列表。
def _index_records(index_path: Path) -> list[dict[str, Any]]:
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


# LLM: _error_payload keeps failed reads metadata-only and omits content.
# 函数用途: 失败时只返回错误码、引用和说明，不泄漏任何文件正文。
def _error_payload(error_code: str, artifact_ref: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "artifact_ref": artifact_ref,
        "error_code": error_code,
        "message": message,
        "reads_artifact_body": False,
    }


# LLM: _tool_output_root returns the only directory where tool-output artifact bodies may live.
# 函数用途: 生成 workspace 内固定 tool_outputs 目录，供 index、路径边界和读取逻辑共用。
def _tool_output_root(root: Path) -> Path:
    return root / "memory_archive" / "artifacts" / "tool_outputs"


# LLM: _is_under_allowed_root prevents registered artifact paths from escaping the trusted artifact directory.
# 函数用途: 判断 artifact 文件是否仍在允许目录下，阻止绝对路径或 .. 越界读取。
def _is_under_allowed_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


# LLM: _looks_like_path decides whether a ref should also be compared as a filesystem path.
# 函数用途: 区分 sha/call_id 这类纯标识和 path-like ref，避免把普通字符串都当路径解析。
def _looks_like_path(value: str) -> bool:
    return any(mark in value for mark in ("/", "\\")) or value.endswith(".json")


# LLM: _sha256_text provides stable content verification for explicit artifact body reads.
# 函数用途: 计算 artifact 正文 UTF-8 sha256，用来核对 index/payload 元数据。
def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
