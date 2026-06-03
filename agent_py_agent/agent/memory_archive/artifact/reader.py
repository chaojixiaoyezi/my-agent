from __future__ import annotations

"""explicit refs-only-to-body reader for externalized tool output artifacts."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...model_visible_ref_sanitizer import sanitize_model_visible_tool_output
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
    path = Path(str(record.get("path", "") or "")).expanduser().resolve(strict=False)
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
    tool = str(payload.get("tool") or record.get("tool") or "")
    model_content = sanitize_model_visible_tool_output(tool, content)
    read_result = read_artifact_content_by_mode(
        ArtifactContentReadRequest(
            content=model_content,
            mode=request.mode,
            offset=request.offset,
            max_chars=request.max_chars,
            query=request.query,
        )
    )
    if not read_result.ok:
        return _error_payload(read_result.error_code, artifact_ref, read_result.message)
    base = _success_base_payload(read, payload, content, digest)
    base["content_sanitized"] = model_content != content
    base.update(_success_content_payload(read_result))
    base.update(read_result.metadata or {})
    return base


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


def _success_content_payload(read_result: ArtifactContentReadResult) -> dict[str, Any]:
    return {
        "read_mode": read_result.mode,
        "content_offset": read_result.offset,
        "content_max_chars": read_result.max_chars,
        "content_chars": len(read_result.content),
        "truncated": read_result.truncated,
        "content": read_result.content,
    }

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
    return [record for record in matches if _record_has_scope(record)]


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
    for index_path in [directory / "index.jsonl" for directory in _tool_output_roots(root)]:
        records.extend(_index_records(index_path))
    return records


def _tool_output_roots(root: Path) -> tuple[Path, ...]:
    return (root / "blobs" / "tool_outputs",)


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
