from __future__ import annotations

"""LLM: archive file reading and record normalization for memory archive queries.

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


def _archive_files(root: Path, *, layer: str, date_key: str | None) -> list[tuple[str, Path]]:
    """LLM: return existing archive files for raw/hook layers in newest-first order.

    新手说明:
    如果指定日期，就只看那天的文件；没指定日期，就看最近若干个 JSONL 文件。
    这样不会为了一个 list/search 命令把多年归档一次性翻完。

    参数说明:
    `root` 是工作区根目录；`layer` 是 `raw`、`hook` 或 `all`；`date_key` 是可选日期。

    返回说明:
    返回 `(layer, path)` 元组列表，按最近优先排列。
    """

    layers = ["raw", "hook"] if layer == "all" else [layer]
    files: list[tuple[str, Path]] = []
    for current_layer in layers:
        directory = _archive_dir(root, current_layer)
        if date_key:
            candidate = directory / f"{date_key}.jsonl"
            if candidate.exists():
                files.append((current_layer, candidate))
            continue
        if directory.exists():
            layer_files = sorted(
                [path for path in directory.glob("*.jsonl") if path.is_file()],
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )[:ARCHIVE_SEARCH_FILE_LIMIT]
            files.extend((current_layer, path) for path in layer_files)
    return files


def _archive_dir(root: Path, layer: str) -> Path:
    """LLM: resolve the directory for one archive layer through public path helpers.

    新手说明:
    目录规则不要散落在 CLI 里。
    hook 用 `snapshot_path_for`，raw 用 `raw_event_path_for`，以后路径变了这里也能跟着变。

    参数说明:
    `root` 是工作区根目录；`layer` 是 `hook` 或 `raw`。

    返回说明:
    返回对应归档目录路径。
    """

    return snapshot_path_for(root).parent if layer == "hook" else raw_event_path_for(root).parent


def _read_archive_file(layer: str, path: Path) -> list[dict[str, Any]]:
    """LLM: parse one archive JSONL file and skip malformed lines without crashing.

    新手说明:
    归档是排障兜底层。
    即使里面有一行坏 JSON，命令也应该继续读其他行，并把坏行标出来，而不是直接中断。

    参数说明:
    `layer` 是当前层名；`path` 是 JSONL 文件路径。

    返回说明:
    返回标准化记录列表；坏行会变成 `archive_error` 记录。
    """

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


def _normalize_archive_record(layer: str, path: Path, line_no: int, payload: dict[str, Any]) -> dict[str, Any]:
    """LLM: map raw event or hook snapshot payloads to a shared search/display shape.

    新手说明:
    raw 事件有 event_id、speaker、tool_name；hook 快照有 snapshot_id、user_intents、next_actions。
    统一后，搜索命令就能按同一套字段工作。

    参数说明:
    `layer` 是 raw/hook；`path` 是来源文件；`line_no` 是行号；`payload` 是原始 JSON 对象。

    返回说明:
    返回标准化归档记录。
    """

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
        "source": str(payload.get("source") or derived.get("source") or ""),
        "archive_level": _archive_level_value(payload.get("archive_level", 3)),
        "created_at": created_at,
        "created_at_sort": _created_at_sort(created_at, fallback=path.stat().st_mtime),
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
    """LLM: keep archive level stable even when the stored value is numeric zero.

    新手说明:
    `0` 是合法 archive level，不能被 Python 的 `or 3` 误判成空值。
    """

    try:
        return int(3 if value is None else value)
    except (TypeError, ValueError):
        return 3


def _gateway_terminal_request_path(request_path: str) -> str:
    """LLM: prefer completed gateway request archive paths over transient processing paths.

    新手说明:
    真实 gateway 会先把请求放在 `requests/processing/`，处理完成后再移动到
    `requests/done/` 或 `requests/failed/`。LocalStore 可能记录的是处理中的临时路径，
    恢复时应该优先指向最终还存在的归档文件，避免第二天按提示去读一个已经被移动走的路径。
    参数说明:
    `request_path` 是 LocalStore metadata 里记录的请求 JSON 路径，可能为空、可能是 processing 路径。
    返回说明:
    返回最适合恢复读取的路径；如果找不到更好的终态文件，就保持原值。
    """

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
