
from __future__ import annotations

"""externalizes large runtime tool outputs into artifact files.

Human version:
Tool results can be useful evidence, but large bodies do not belong in raw
archive rows, token ledgers, or compact metadata. This module writes the full
tool output to an artifact file and returns a compact record with preview,
hash, size, and path. Internal orchestration/status tool outputs are stored in
their original form.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.path_segments import safe_path_segment
from ..settings.defaults import default_agent_config
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

TOOL_OUTPUT_RECORD_SCHEMA = RuntimeMemorySchemaOptions("tool_output_archive_record")
TOOL_OUTPUT_ARTIFACT_SCHEMA = RuntimeMemorySchemaOptions("tool_output_artifact")
TOOL_OUTPUT_INDEX_SCHEMA = RuntimeMemorySchemaOptions("tool_output_index")


@dataclass(frozen=True)
class ExternalizeToolOutputRequest:
    root: str | Path
    tool: str
    call_id: str
    output: str
    ok: bool
    status: str = ""
    error_code: str = ""
    run_id: str = ""
    task_id: str = ""
    request_id: str = ""
    min_chars: int = -1
    preview_chars: int = -1
    parameters: dict[str, Any] | None = None
    result_envelope: dict[str, Any] | None = None


def externalize_tool_output_record(request: ExternalizeToolOutputRequest) -> dict[str, Any]:
    output = str(request.output or "")
    digest = _sha256_text(output)
    resolved = _resolved_request_limits(request)
    record = _base_record(request, output, digest, preview_chars=resolved.preview_chars)
    if _is_bounded_read_artifact_output(request, output):
        record.update(_read_artifact_record_fields(output))
        return record
    if _is_bounded_read_file_output(request):
        record.update(_archive_bounded_read_file_output(request, output, resolved))
        if not record.get("output_externalized") and not record.get("source_output_archived"):
            _append_tool_call_index(request, record, digest)
        return record
    if _output_meets_archive_threshold(output, resolved.min_chars):
        path = _write_output_artifact(request, output, digest)
        record.update({
            "output_externalized": True,
            "output_path": str(path),
            "artifact_ref": str(path),
        })
    else:
        _append_tool_call_index(request, record, digest)
    return record


def _archive_bounded_read_file_output(
    request: ExternalizeToolOutputRequest,
    output: str,
    resolved: _ResolvedOutputLimits,
) -> dict[str, Any]:
    if not _output_meets_archive_threshold(output, resolved.min_chars):
        return {
            "source_output_archived": False,
            "source_output_path": "",
            "source_artifact_ref": "",
        }
    path = _write_output_artifact(request, output, _sha256_text(output))
    return {
        "output_externalized": False,
        "output_path": "",
        "artifact_ref": str(path),
        "source_output_archived": True,
        "source_output_path": str(path),
        "source_artifact_ref": str(path),
    }


@dataclass(frozen=True)
class _ResolvedOutputLimits:
    min_chars: int
    preview_chars: int


def _resolved_request_limits(request: ExternalizeToolOutputRequest) -> _ResolvedOutputLimits:
    defaults = default_agent_config()
    return _ResolvedOutputLimits(
        min_chars=_request_limit(request.min_chars, defaults.tool_output_externalize_min_chars),
        preview_chars=_request_limit(request.preview_chars, defaults.tool_output_preview_chars),
    )


def _request_limit(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = -1
    return int(default) if parsed < 0 else max(0, parsed)


def _output_meets_archive_threshold(output: str, min_chars: int) -> bool:
    threshold = max(0, int(min_chars))
    if threshold <= 0:
        return True
    return max(len(output), len(output.encode("utf-8"))) >= threshold


def _request_status(request: ExternalizeToolOutputRequest) -> str:
    status = str(request.status or "").strip()
    if status:
        return status
    return "ok" if request.ok else "error"


def _base_record(request: ExternalizeToolOutputRequest, output: str, digest: str, *, preview_chars: int) -> dict[str, Any]:
    record = {
        "version": TOOL_OUTPUT_RECORD_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_RECORD_SCHEMA),
        "tool": request.tool,
        "id": request.call_id,
        "call_id": request.call_id,
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "scoped_call_id": _scoped_call_id(request),
        "ok": request.ok,
        "status": _request_status(request),
        "error_code": str(request.error_code or "").strip(),
        "output_preview": _preview(output, preview_chars),
        "output_hash": digest,
        "output_size_bytes": len(output.encode("utf-8")),
        "output_externalized": False,
        "output_path": "",
    }
    if read_window := _read_window_from_envelope(request.result_envelope):
        record["read_window"] = read_window
    return record


def _write_output_artifact(request: ExternalizeToolOutputRequest, output: str, digest: str) -> Path:
    path = _artifact_path(request, digest)
    created_at = datetime.now(tz=timezone.utc).isoformat()
    payload = {
        "version": TOOL_OUTPUT_ARTIFACT_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_ARTIFACT_SCHEMA),
        "kind": "tool_output",
        "tool": request.tool,
        "call_id": request.call_id,
        "scoped_call_id": _scoped_call_id(request),
        "ok": request.ok,
        "status": _request_status(request),
        "error_code": str(request.error_code or "").strip(),
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "parameters": _safe_parameters(request.parameters),
        **({"read_window": read_window} if (read_window := _read_window_from_envelope(request.result_envelope)) else {}),
        "source_input": _source_input(request.parameters),
        "sha256": digest,
        "size_bytes": len(output.encode("utf-8")),
        "created_at": created_at,
        "content": output,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _append_index(path, payload)
    return path


def _append_index(path: Path, payload: dict[str, Any]) -> None:
    record = {
        "version": TOOL_OUTPUT_INDEX_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_INDEX_SCHEMA),
        "kind": payload["kind"],
        "tool": payload["tool"],
        "call_id": payload["call_id"],
        "scoped_call_id": payload.get("scoped_call_id", ""),
        "request_id": payload["request_id"],
        "run_id": payload["run_id"],
        "task_id": payload["task_id"],
        "ok": bool(payload.get("ok")),
        "status": str(payload.get("status") or ""),
        "error_code": str(payload.get("error_code") or ""),
        "parameters": _safe_parameters(payload.get("parameters")),
        **({"read_window": payload["read_window"]} if isinstance(payload.get("read_window"), dict) else {}),
        "source_input": str(payload.get("source_input") or ""),
        "path": str(path),
        "sha256": payload["sha256"],
        "size_bytes": payload["size_bytes"],
        "created_at": payload["created_at"],
    }
    index_path = path.parent / "index.jsonl"
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _append_tool_call_index(request: ExternalizeToolOutputRequest, record: dict[str, Any], digest: str) -> None:
    created_at = datetime.now(tz=timezone.utc).isoformat()
    payload = {
        "version": TOOL_OUTPUT_INDEX_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_INDEX_SCHEMA),
        "kind": "tool_call",
        "tool": request.tool,
        "call_id": request.call_id,
        "scoped_call_id": _scoped_call_id(request),
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "ok": request.ok,
        "status": str(record.get("status") or _request_status(request)),
        "error_code": str(record.get("error_code") or request.error_code or "").strip(),
        "parameters": _safe_parameters(request.parameters),
        **({"read_window": record["read_window"]} if isinstance(record.get("read_window"), dict) else {}),
        "source_input": _source_input(request.parameters),
        "path": "",
        "sha256": digest,
        "size_bytes": int(record.get("output_size_bytes", 0) or 0),
        "output_externalized": False,
        "created_at": created_at,
    }
    index_path = Path(request.root) / "blobs" / "tool_outputs" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _artifact_path(request: ExternalizeToolOutputRequest, digest: str) -> Path:
    return (
        Path(request.root)
        / "blobs"
        / "tool_outputs"
        / (
            f"{safe_path_segment(request.tool, default='item', replacement='_')}-"
            f"{safe_path_segment(request.call_id, default='item', replacement='_')}-{digest[:12]}.json"
        )
    )


def _preview(output: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(output) <= max_chars:
        return output
    return output[:max_chars] + f"\n... [truncated {len(output) - max_chars} chars]"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_window_from_envelope(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    window = value.get("read_window")
    if not isinstance(window, dict):
        return {}
    kind = str(window.get("kind") or "").strip()
    if kind == "char_window":
        return _char_window(window)
    if kind == "line_window":
        return _line_window(window)
    return {}


def _char_window(window: dict[str, object]) -> dict[str, object]:
    offset = _optional_nonnegative_int(window.get("offset"))
    chars = _optional_nonnegative_int(window.get("chars"))
    total = _optional_nonnegative_int(window.get("total_chars"))
    next_offset = _optional_nonnegative_int(window.get("next_offset"))
    if offset is None or total is None:
        return {}
    if chars is None and next_offset is not None:
        chars = max(0, next_offset - offset)
    if next_offset is None and chars is not None:
        next_offset = offset + chars
    if chars is None or next_offset is None:
        return {}
    return {
        "kind": "char_window",
        "offset": offset,
        "chars": chars,
        "next_offset": next_offset,
        "total_chars": total,
        "complete": bool(window.get("complete")) or bool(total and next_offset >= total),
    }


def _line_window(window: dict[str, object]) -> dict[str, object]:
    start = _optional_positive_int(window.get("start_line"))
    end = _optional_nonnegative_int(window.get("end_line"))
    total = _optional_nonnegative_int(window.get("total_lines"))
    next_start = _optional_nonnegative_int(window.get("next_start_line"))
    if start is None or end is None or total is None:
        return {}
    if next_start is None:
        next_start = end + 1 if end < total else 0
    return {
        "kind": "line_window",
        "start_line": start,
        "end_line": end,
        "next_start_line": next_start,
        "total_lines": total,
        "complete": bool(window.get("complete")) or bool(total and start <= 1 and end >= total),
    }


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _optional_positive_int(value: object) -> int | None:
    number = _optional_nonnegative_int(value)
    return number if number and number > 0 else None


def _is_bounded_read_artifact_output(request: ExternalizeToolOutputRequest, output: str) -> bool:
    if request.tool != "read_artifact" or not request.ok:
        return False
    payload = _json_object(output)
    return bool(payload and payload.get("reads_artifact_body") is True)


def _is_bounded_read_file_output(request: ExternalizeToolOutputRequest) -> bool:
    return request.tool == "read_file" and request.ok


def _read_artifact_record_fields(output: str) -> dict[str, Any]:
    payload = _json_object(output) or {}
    return {
        "reads_artifact_body": True,
        "source_artifact_ref": str(payload.get("artifact_ref") or ""),
        "source_tool": str(payload.get("tool") or ""),
        "source_call_id": str(payload.get("call_id") or ""),
    }


def _safe_parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        text_key = str(key).strip()
        if not text_key:
            continue
        if (safe_item := _safe_parameter_item(item)) is not _UNSAFE_PARAMETER:
            result[text_key] = safe_item
    return result


_UNSAFE_PARAMETER = object()


def _safe_parameter_item(item: Any) -> object:
    if isinstance(item, str | int | float | bool) or item is None:
        return item
    if isinstance(item, list | tuple):
        return [entry for entry in item if isinstance(entry, str | int | float | bool) or entry is None][:20]
    if isinstance(item, dict):
        return {
            str(child_key): child_value
            for child_key, child_value in list(item.items())[:20]
            if isinstance(child_value, str | int | float | bool) or child_value is None
        }
    return _UNSAFE_PARAMETER


def _source_input(value: Any) -> str:
    params = _safe_parameters(value)
    for key in ("path", "url", "artifact_ref", "query", "command"):
        candidate = str(params.get(key) or "").strip()
        if candidate:
            return candidate
    for item in params.values():
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _scoped_call_id(request: ExternalizeToolOutputRequest) -> str:
    scope = request.run_id or request.task_id or request.request_id
    return f"{scope}:{request.call_id}" if scope else request.call_id


__all__ = ["ExternalizeToolOutputRequest", "externalize_tool_output_record"]
