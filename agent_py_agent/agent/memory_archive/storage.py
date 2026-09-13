
from __future__ import annotations

from ..common.json_io import jsonl_lines

"""JSONL storage primitives for memory hook snapshots and raw archive events.

新手说明:
这个文件只管把快照和归档事件写到固定目录。
它不决定什么时候压缩、不调用模型，也不读取配置；以后接入真实流程时，外层负责判断时机，这里负责可靠落盘。
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from ._storage_dates import (
    _coerce_retention_days,
    _coerce_today,
    _date_from_filename,
    _date_key,
)
from .models import CompressionSnapshot, RawMemoryEvent


class MemoryArchiveError(RuntimeError):
    """Raised when archive writes cannot be verified after append."""


def snapshot_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:

    return Path(root) / "memory" / "hooks" / f"{_date_key(created_at)}.jsonl"


def compression_snapshot_dir(root: str | Path) -> Path:

    return Path(root) / "memory_archive" / "snapshots"


def compression_snapshot_file_for(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in snapshot.snapshot_id)
    return compression_snapshot_dir(root) / f"{_date_key(snapshot.created_at)}--{safe_id}.json"


def raw_event_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:

    return Path(root) / "audit" / f"{_date_key(created_at)}.jsonl"


def filter_snapshot_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 1:
        _clear_tool_parameter_previews(result)
    if level >= 2:
        result["user_intents"] = result.get("user_intents", [])[:1]
        result["assistant_actions"] = result.get("assistant_actions", [])[:1]
        result["decisions"] = []
        result["open_questions"] = []
    if level >= 3:
        result["user_intents"] = []
        result["assistant_actions"] = []
        result["tool_calls"] = []
        result["content"] = ""
    return result


def _clear_tool_parameter_previews(payload: dict[str, Any]) -> None:
    for tc in payload.get("tool_calls", []):
        if isinstance(tc, dict) and "parameters_preview" in tc:
            tc["parameters_preview"] = ""


def filter_raw_event_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 2:
        result["content_path"] = ""
    if level >= 3:
        result["content_path"] = ""
    return result


def append_snapshot(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    path = snapshot_path_for(root, snapshot.created_at)
    payload = snapshot.to_dict()
    append_jsonl(path, payload, sort_keys=True)
    _verify_record_exists(path, key="snapshot_id", value=snapshot.snapshot_id, expected=payload)
    return path


def write_compression_snapshot_file(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    path = compression_snapshot_file_for(root, snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot.to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    _verify_json_file_payload(path, expected=payload)
    return path


def append_raw_event(root: str | Path, event: RawMemoryEvent) -> Path:

    path = raw_event_path_for(root, event.created_at)
    payload = filter_raw_event_for_level(event.to_dict(), event.archive_level)
    append_jsonl(path, payload, sort_keys=True)
    _verify_record_exists(path, key="event_id", value=event.event_id, expected=payload)
    return path


def _verify_record_exists(path: Path, *, key: str, value: str, expected: dict[str, Any]) -> None:
    """Read a JSONL file backwards and confirm the just-written record is present."""
    normalized_expected = _normalized_json(expected)
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    for line in jsonl_lines(reversed(path.read_text(encoding="utf-8"))):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get(key) != value:
            continue
        if _normalized_json(payload) != normalized_expected:
            raise MemoryArchiveError(f"readback payload mismatch for {key}={value}")
        return
    raise MemoryArchiveError(f"readback failed for {key}={value} in {path}")


def _verify_json_file_payload(path: Path, *, expected: dict[str, Any]) -> None:
    """Ensure an authoritative JSON snapshot file can be read back exactly."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryArchiveError(f"readback failed for snapshot file {path}: {exc}") from exc
    if _normalized_json(payload) != _normalized_json(expected):
        raise MemoryArchiveError(f"readback payload mismatch for snapshot file {path}")


def _normalized_json(payload: dict[str, Any]) -> str:
    """Serialize JSON payloads into a canonical string for equality checks."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def enforce_retention(root: str | Path, retention_days: Any, today: date | str | None = None) -> list[Path]:

    days = _coerce_retention_days(retention_days)
    if days is None or days == 0:
        return []

    hook_dir = Path(root) / "memory" / "hooks"
    if not hook_dir.exists():
        return []

    today_date = _coerce_today(today) or date.today()
    cutoff = today_date - timedelta(days=days - 1)
    deleted: list[Path] = []

    for path in sorted(hook_dir.glob("*.jsonl")):
        file_date = _date_from_filename(path)
        if file_date is None or file_date >= cutoff:
            continue
        path.unlink()
        deleted.append(path)

    return deleted
