from __future__ import annotations

"""JSON loading helpers for task-local compact continuation refs."""

import json
from pathlib import Path
from typing import Any

from ...runtime_errors import runtime_error_report

_MAX_PACKET_JSON_CHARS = 200_000
_DEFAULT_SNIPPET_CHARS = 1200


def read_json(path: Path) -> dict[str, Any]:
    return read_json_with_status(path)[0]


def read_json_with_status(
    path: Path,
    *,
    context: str = "subagent_compact_continuation.json",
) -> tuple[dict[str, Any], str, dict[str, object] | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {}, "unreadable_json", _json_load_error(path, exc, context)
    if len(raw) > _MAX_PACKET_JSON_CHARS:
        exc = ValueError(
            f"JSON file exceeds compact continuation read budget: {len(raw)} > {_MAX_PACKET_JSON_CHARS}"
        )
        return {}, "unreadable_json", _json_load_error(path, exc, context)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, "unreadable_json", _json_load_error(path, exc, context)
    if not isinstance(payload, dict):
        exc = ValueError(f"JSON root is {type(payload).__name__}, expected object")
        return {}, "unreadable_json", _json_load_error(path, exc, context)
    return payload, "ok", None


def read_text(path: Path, max_chars: int) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    text = _pretty_json_text(raw) if path.suffix == ".json" else raw
    limit = max(200, int(max_chars or _DEFAULT_SNIPPET_CHARS))
    if len(text) <= limit:
        return text.strip()
    return f"{text[:limit].rstrip()}\n... [truncated; read ref for full content]"


def short_value(value: Any, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    limit = max(80, min(int(max_chars or _DEFAULT_SNIPPET_CHARS), 300))
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


def load_error_lines(
    label: str,
    load_error: dict[str, object] | None,
    max_chars: int,
) -> list[str]:
    if not load_error:
        return []
    lines = [f"- {label}:"]
    for key in ("context", "path", "category", "error_type", "recoverable", "model_message"):
        if key in load_error:
            lines.append(f"  - {key}: {short_value(load_error[key], max_chars)}")
    return lines


def _json_load_error(path: Path, exc: BaseException, context: str) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


def _pretty_json_text(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = ["load_error_lines", "read_json", "read_json_with_status", "read_text", "short_value"]
