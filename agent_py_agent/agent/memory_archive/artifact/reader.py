from __future__ import annotations

"""explicit refs-only-to-body reader for externalized tool output artifacts."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.tool_output_paths import (
    tool_output_index_paths_for_lookup,
    tool_output_roots_for_lookup,
)
from .read_modes import (
    ArtifactContentReadRequest,
    ArtifactContentReadResult,
    read_artifact_content_by_mode,
)


@dataclass(frozen=True)
class ReadToolOutputArtifactRequest:
    root: str | Path
    artifact_ref: str
    offset: int = 0
    max_chars: int = -1
    mode: str = "slice"
    query: str = ""
    run_id: str = ""
    task_id: str = ""
    request_id: str = ""
    scope_mode: str = ""


@dataclass(frozen=True)
class _RegisteredArtifactRead:
    path: Path
    record: dict[str, Any]
    request: ReadToolOutputArtifactRequest


def read_tool_output_artifact(request: ReadToolOutputArtifactRequest) -> dict[str, Any]:
    root = Path(request.root).expanduser().resolve(strict=False)
    artifact_ref = str(request.artifact_ref or "").strip()
    if not artifact_ref:
        return _error_payload("missing_artifact_ref", artifact_ref, "artifact_ref is required")
    record = _find_index_record(root, artifact_ref, request)
    if record is None:
        return _error_payload("artifact_not_registered", artifact_ref, "artifact ref was not found in tool output index")
    if not _record_allowed_for_read_scope(record, request):
        return _error_payload(
            "tool_permission_denied",
            artifact_ref,
            "artifact ref belongs to another run and is outside the current read scope",
        )
    registered_path = str(record.get("path", "") or "").strip()
    if not registered_path:
        # 记录命中(scoped_call_id 在 index 里)但 path 为空 —— 该工具输出从未外置成可读 blob
        # (compaction 期间 output_externalized=false / CONTEXT_COMPACT_DEFERRED,或低于外置阈值)。
        # 旧逻辑让空 path 落成 Path("")→cwd→误报 artifact_path_outside_tool_outputs,模型照
        # reducer policy"use scoped_call_id for read_artifact"反复重试同一个读不到的 ref。改为返回
        # 明确语义 + 原始来源,让模型直接 read_file 源(真机 stage4:每 compaction 周期省 ~3 轮瞎试)。
        source = _record_source_hint(record)
        message = (
            "该工具输出未外置成可读 artifact(output_externalized=false);"
            + (f"直接 read_file 原始来源:{source}" if source else "请改用直接读取(read_file/重跑工具)推进,勿重试该 ref")
        )
        return _error_payload("artifact_not_externalized", artifact_ref, message)
    path = Path(registered_path).expanduser().resolve(strict=False)
    allowed_roots = _tool_output_roots(root)
    if not any(_is_under_allowed_root(path, allowed_root) for allowed_root in allowed_roots):
        return _error_payload("artifact_path_outside_tool_outputs", artifact_ref, "registered path is outside tool_outputs")
    if not path.is_file():
        return _error_payload("artifact_missing", artifact_ref, "registered artifact file does not exist")
    return _read_registered_artifact(_RegisteredArtifactRead(path=path, record=record, request=request))


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


# LLM: The reader may retain host-only path/scope facts for CLI and audit, but it must also expose a
# stable logical ref so the tool layer can project a path-free continuation contract.
# 函数用途: 组装读取成功的完整宿主事实，并附上后续模型可安全复用的逻辑引用。
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
        "canonical_artifact_ref": str(
            record.get("scoped_call_id")
            or record.get("call_id")
            or artifact_ref
        ),
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


# LLM: Model-facing callers need explicit window direction. Tail reads reach the real end even when
# they omit a prefix, while slice/head expose next_offset only when more content follows.
# 函数用途: 把正文读取结果转换成不含糊的窗口与续读信息。
def _success_content_payload(read_result: ArtifactContentReadResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "read_mode": read_result.mode,
        "content_offset": read_result.offset,
        "content_max_chars": read_result.max_chars,
        "content_chars": len(read_result.content),
        "total_chars": read_result.total_chars,
        "has_more_before": read_result.has_more_before,
        "has_more_after": read_result.has_more_after,
        "truncated": read_result.truncated,
        "content": read_result.content,
    }
    if read_result.mode != "search":
        payload["window_start"] = read_result.offset
        payload["window_end"] = read_result.offset + len(read_result.content)
        if read_result.has_more_after:
            payload["next_offset"] = payload["window_end"]
    return payload

def _find_index_record(
    root: Path,
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
) -> dict[str, Any] | None:
    records = _index_records_for_root(root)
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
    if matches and _request_has_scope(request):
        if resolved_ref is not None and not _request_has_strong_scope(request):
            return matches[-1]
        return None
    if matches:
        return matches[-1]
    if resolved_ref is not None:
        return _unique_record_by_basename(records, ref_path.name)
    return None


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
    if _request_has_scope(request):
        return [] if any(_record_has_scope(record) for record in matches) else matches
    return [record for record in matches if _record_has_scope(record)]


def _request_has_scope(request: ReadToolOutputArtifactRequest) -> bool:
    return any(str(getattr(request, key) or "").strip() for key in ("run_id", "task_id", "request_id"))


def _request_has_strong_scope(request: ReadToolOutputArtifactRequest) -> bool:
    return any(str(getattr(request, key) or "").strip() for key in ("task_id", "request_id"))


def _record_scope_matches_request(
    record: dict[str, Any],
    request: ReadToolOutputArtifactRequest,
) -> bool:
    return any(
        str(getattr(request, key) or "").strip()
        and str(record.get(key) or "").strip() == str(getattr(request, key) or "").strip()
        for key in ("run_id", "task_id", "request_id")
    )


def _record_has_scope(record: dict[str, Any]) -> bool:
    return any(str(record.get(key) or "").strip() for key in ("run_id", "task_id", "request_id"))


# LLM: A current-run artifact scope accepts only records carrying the exact
# trusted run id injected by the gateway; missing scope fails closed.
# 函数用途: 防止同一任务下的来源工作者通过猜 ref 读取兄弟子代理工具输出。
def _record_allowed_for_read_scope(
    record: dict[str, Any],
    request: ReadToolOutputArtifactRequest,
) -> bool:
    if str(request.scope_mode or "").strip().lower() != "current_run":
        return True
    run_id = str(request.run_id or "").strip()
    return bool(
        run_id
        and str(record.get("run_id") or "").strip() == run_id
    )


def _unique_record_by_basename(records: list[dict[str, Any]], filename: str) -> dict[str, Any] | None:
    if not filename.endswith(".json"):
        return None
    matches = [
        record
        for record in records
        if Path(str(record.get("path", "") or "")).name == filename
    ]
    return matches[0] if len(matches) == 1 else None


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


def _record_source_hint(record: dict[str, Any]) -> str:
    # 未外置记录里仍留着"这条 tool output 当初读/跑的是什么"——优先 source_input,其次原始
    # 参数里的 path,给模型一个能直接 read_file 的具体来源,免去 compaction 后瞎找。
    params = record.get("parameters") if isinstance(record.get("parameters"), dict) else {}
    return str(
        record.get("source_input")
        or record.get("source_path")
        or (params.get("path") if isinstance(params, dict) else "")
        or ""
    ).strip()


def _error_payload(error_code: str, artifact_ref: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "artifact_ref": artifact_ref,
        "error_code": error_code,
        "message": message,
        "reads_artifact_body": False,
    }


def _index_records_for_root(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index_path in tool_output_index_paths_for_lookup(root):
        records.extend(_index_records(index_path))
    return records


def _tool_output_roots(root: Path) -> tuple[Path, ...]:
    return tool_output_roots_for_lookup(root)


def _is_under_allowed_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _looks_like_path(value: str) -> bool:
    return any(mark in value for mark in ("/", "\\")) or value.endswith(".json")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
