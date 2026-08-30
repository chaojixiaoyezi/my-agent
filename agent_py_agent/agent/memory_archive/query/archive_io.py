
from __future__ import annotations

"""archive file reading and record normalization for memory archive queries.

新手说明:
这个文件负责"从磁盘读归档文件"和"把原始 JSON 记录变统一格式"。
读文件、找目录、解析 JSONL、标准化字段都放在这里。
它依赖 archive_helpers 里的纯计算工具，但不依赖 query_logic。
"""

import json
from pathlib import Path
from typing import Any

from ..storage import raw_event_path_for, snapshot_path_for
from .archive_helpers import (
    _archive_error_record,
    _archive_preview,
    _created_at_sort,
    _derived_archive_fields,
    _list_value,
)

ARCHIVE_SEARCH_FILE_LIMIT = 30


def _archive_files(
    root: Path,
    *,
    layer: str,
    date_key: str | None,
    file_limit: int = ARCHIVE_SEARCH_FILE_LIMIT,
) -> list[tuple[str, Path]]:

    layers = ["raw", "hook"] if layer == "all" else [layer]
    files: list[tuple[str, Path]] = []
    for current_layer in layers:
        directory = _archive_dir(root, current_layer)
        if date_key:
            files.extend(_dated_layer_file(current_layer, directory, date_key))
            continue
        if directory.exists():
            files.extend(_recent_layer_files(current_layer, directory, file_limit=file_limit))
    return files


def _dated_layer_file(layer: str, directory: Path, date_key: str) -> list[tuple[str, Path]]:
    candidate = directory / f"{date_key}.jsonl"
    return [(layer, candidate)] if candidate.exists() else []


def _recent_layer_files(
    layer: str,
    directory: Path,
    *,
    file_limit: int = ARCHIVE_SEARCH_FILE_LIMIT,
) -> list[tuple[str, Path]]:
    layer_files = sorted(
        [path for path in directory.glob("*.jsonl") if path.is_file()],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )[: max(0, int(file_limit))]
    return [(layer, path) for path in layer_files]


def _archive_dir(root: Path, layer: str) -> Path:

    return snapshot_path_for(root).parent if layer == "hook" else raw_event_path_for(root).parent


def _read_archive_file(layer: str, path: Path) -> list[dict[str, Any]]:

    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [_archive_error_record(layer, path, line_no=0, message=f"{type(exc).__name__}: {exc}")]
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            records.append(_archive_error_record(layer, path, line_no=line_no, message=str(exc)))
            continue
        if not isinstance(payload, dict):
            records.append(_archive_error_record(layer, path, line_no=line_no, message="record is not a JSON object"))
            continue
        records.append(_normalize_archive_record(layer, path, line_no, payload))
    return records


# LLM: Query normalization must preserve typed operation/effect identity from raw events while
# retaining the original payload for diagnostics; these fields are evidence, not prose aliases.
# 函数用途: 把一条原始归档记录整理成查询接口的统一字段，并保留工具操作的结构化证据。
def _normalize_archive_record(layer: str, path: Path, line_no: int, payload: dict[str, Any]) -> dict[str, Any]:

    derived = _derived_archive_fields(payload)
    created_at = str(payload.get("created_at", "") or "")
    record_id = str(payload.get("event_id") or payload.get("snapshot_id") or f"{path.name}:{line_no}")
    return {
        "layer": layer,
        "kind": "hook_snapshot" if layer == "hook" else "raw_event",
        "id": record_id,
        "session_id": str(payload.get("session_id", "") or ""),
        "request_id": str(payload.get("request_id") or derived.get("request_id") or ""),
        "run_id": str(payload.get("run_id") or derived.get("run_id") or ""),
        "task_id": str(payload.get("task_id") or derived.get("task_id") or ""),
        "speaker": str(payload.get("speaker", "") or ""),
        "target": str(payload.get("target", "") or ""),
        "action": str(payload.get("action", "snapshot" if layer == "hook" else "") or ""),
        "status": str(payload.get("status") or derived.get("status") or ""),
        "error_code": str(payload.get("error_code") or derived.get("error_code") or ""),
        "is_dispatch": bool(payload.get("is_dispatch", False)),
        "tool_name": str(payload.get("tool_name", "") or ""),
        "tool_success": payload.get("tool_success"),
        "operation_id": str(payload.get("operation_id", "") or ""),
        "effect_outcome": str(payload.get("effect_outcome", "") or ""),
        "source_ref": str(payload.get("source_ref", "") or ""),
        "source": str(payload.get("source") or derived.get("source") or ""),
        "archive_level": _archive_level_value(payload.get("archive_level", 3)),
        "created_at": created_at,
        "created_at_sort": _created_at_sort(created_at, default=path.stat().st_mtime),
        "content_preview": _archive_preview(payload),
        "content_path": str(payload.get("content_path", "") or ""),
        "content_hash": str(payload.get("content_hash", "") or ""),
        "task_refs": [str(item) for item in _list_value(payload.get("task_refs"))],
        "next_actions": [str(item) for item in _list_value(payload.get("next_actions"))],
        "file_path": str(path),
        "line_no": line_no,
        "payload": payload,
    }


def _archive_level_value(value: Any) -> int:

    try:
        return int(3 if value is None else value)
    except (TypeError, ValueError):
        return 3


def _gateway_terminal_request_path(request_path: str) -> str:

    if not request_path:
        return ""
    path = Path(request_path)
    parts = list(path.parts)
    try:
        requests_index = parts.index("requests")
        state_index = requests_index + 1
    except ValueError:
        if path.exists():
            return request_path
        return request_path
    if state_index >= len(parts) or parts[state_index] != "processing":
        if path.exists():
            return request_path
        return request_path
    candidates: list[Path] = []
    for terminal_state in ("done", "failed"):
        updated_parts = parts[:]
        updated_parts[state_index] = terminal_state
        candidates.append(Path(*updated_parts))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return request_path
