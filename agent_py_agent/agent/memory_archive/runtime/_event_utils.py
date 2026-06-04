
"""Pure utility helpers for runtime raw archive event builders."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

_PREVIEW_LIMITS = {
    0: 2048,
    1: 1024,
    2: 512,
    3: 160,
}


def _summarize_text(content: str, *, default: str, limit: int = 96) -> str:
    """Build a short deterministic summary when full previews should not be stored."""
    compact = " ".join(str(content).split())
    if not compact:
        return default
    limit = max(0, int(limit))
    short = compact[:limit]
    if len(compact) > limit:
        short += "..."
    return short


def _normalize_tool_call(call: Any) -> dict[str, Any]:
    """Coerce arbitrary tool-call objects into a plain dictionary."""
    if isinstance(call, Mapping):
        return dict(call)
    if is_dataclass(call) and not isinstance(call, type):
        return asdict(call)
    if hasattr(call, "__dict__"):
        return {key: value for key, value in vars(call).items() if not key.startswith("_")}
    return {"value": str(call)}


def _normalize_archive_level(value: int) -> int:
    """Keep archive levels inside the supported 0..3 range."""
    if isinstance(value, bool):
        return 3
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 3
    if level < 0 or level > 3:
        return 3
    return level


def _preview(content: str, archive_level: int, preview_limits: dict[int, int] | None = None) -> str:
    """Trim archived text according to archive level."""
    limits = preview_limits or _PREVIEW_LIMITS
    limit = int(limits.get(_normalize_archive_level(archive_level), _PREVIEW_LIMITS[3]))
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return f"{content[: limit - 3]}..."


def _event_id(payload: Mapping[str, Any]) -> str:
    """Derive a stable raw event id from canonical payload facts."""
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"raw:{digest[:32]}"


def _content_hash(content: str) -> str:
    """Compute a SHA-256 content hash with an explicit prefix."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_json(payload: Any) -> str:
    """Serialize payloads into deterministic JSON for identity hashes."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_display_json(payload: Any) -> str:
    """Serialize metadata for human-facing previews while keeping input order."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"), default=str)


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    """Find the first non-empty text value among candidate keys."""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _first_bool(payload: Mapping[str, Any], *keys: str) -> bool | None:
    """Find the first boolean-ish status among candidate keys."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        parsed = _bool_from_text(value) if isinstance(value, str) else None
        if parsed is not None:
            return parsed
    return None


def _bool_from_text(value: str) -> bool | None:
    text = value.strip().lower()
    if text in {"true", "1", "yes", "ok", "success"}:
        return True
    if text in {"false", "0", "no", "error", "failed", "failure"}:
        return False
    return None


def _tool_status(payload: Mapping[str, Any], tool_success: bool | None) -> str:
    """Choose a stable tool status string."""
    status = _first_text(payload, "status")
    if status:
        return status
    if tool_success is True:
        return "ok"
    if tool_success is False:
        return "error"
    return "unknown"
