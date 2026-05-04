from __future__ import annotations

"""Internal helpers for snapshots.py — kept separate to let main functions stay under line limits."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

SNAPSHOT_PREVIEW_LIMITS = {
    0: 2048,
    1: 1024,
    2: 512,
    3: 160,
}


def _normalize_archive_level(value: int) -> int:
    """Normalize snapshot archive level into the supported 0..3 range."""
    if isinstance(value, bool):
        return 3
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 3
    return level if 0 <= level <= 3 else 3


def _preview(content: str, archive_level: int) -> str:
    """Trim snapshot text fields according to archive level."""
    limit = SNAPSHOT_PREVIEW_LIMITS[_normalize_archive_level(archive_level)]
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return f"{content[: limit - 3]}..."


def _dedupe_texts(values: Iterable[str]) -> list[str]:
    """Keep non-empty text values once while preserving order."""
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _snapshot_id(payload: Mapping[str, Any]) -> str:
    """Derive a stable snapshot id from the key recovery facts."""
    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return f"snapshot:{digest[:32]}"


def _content_hash(content: str) -> str:
    """Compute a content hash for snapshot identity."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _stable_json(payload: Any) -> str:
    """Serialize arbitrary payloads into deterministic JSON."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _tool_snapshot(tool_call: Mapping[str, Any], archive_level: int) -> dict[str, Any]:
    """Convert a tool call record into small recovery-safe metadata."""
    parameters = tool_call.get("parameters", {})
    return {
        "tool": str(tool_call.get("tool") or tool_call.get("tool_name") or "unknown"),
        "tool_call_id": str(tool_call.get("id") or tool_call.get("tool_call_id") or ""),
        "ok": tool_call.get("ok"),
        "status": str(tool_call.get("status") or ""),
        "error_code": str(tool_call.get("error_code") or ""),
        "parameters_preview": _preview(_stable_json(parameters), archive_level),
    }


def _participants(tool_calls: list[dict[str, Any]]) -> list[str]:
    """Derive snapshot participants from whether tools appeared."""
    participants = ["user", "assistant"]
    if tool_calls:
        participants.append("tool")
    return participants