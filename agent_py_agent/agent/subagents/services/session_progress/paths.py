
from __future__ import annotations

import json
from typing import Any

WRITE_TOOLS = {"write_file", "apply_patch"}


def progress_path(request: object) -> str:
    tool = str(getattr(request, "tool", "") or "")
    for value in _explicit_progress_path_values(getattr(request, "result_envelope", {})):
        path = path_text(value)
        if path:
            return path
    output_payload = _json_object_from_text(str(getattr(request, "output", "") or ""))
    for value in _explicit_progress_path_values(output_payload):
        path = path_text(value)
        if path:
            return path
    if tool not in WRITE_TOOLS:
        return ""
    for value in _candidate_path_values(getattr(request, "result_envelope", {})):
        path = path_text(value)
        if path:
            return path
    for value in _candidate_path_values(output_payload):
        path = path_text(value)
        if path:
            return path
    return _payload_progress_path(request)


def _payload_progress_path(request: object) -> str:
    tool = str(getattr(request, "tool", "") or "")
    payload = getattr(request, "payload", {})
    payload = payload if isinstance(payload, dict) else {}
    if tool in WRITE_TOOLS:
        return path_text(payload.get("path"))
    return ""


def _candidate_path_values(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        return []
    values = [payload.get(key) for key in _PATH_KEYS if key in payload]
    for key in ("output", "result", "artifact"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            values.extend(_candidate_path_values(nested))
    return values


def _explicit_progress_path_values(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        return []
    values = [payload.get(key) for key in _EXPLICIT_PROGRESS_PATH_KEYS if key in payload]
    for key in ("output", "result", "artifact"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            values.extend(_explicit_progress_path_values(nested))
    return values


_EXPLICIT_PROGRESS_PATH_KEYS = (
    "progress_path",
    "product_path",
    "deliverable_path",
    "user_artifact_path",
)


_PATH_KEYS = (
    "artifact_ref",
    "artifact_path",
    "file_path",
    "target_path",
    "path",
    "output_path",
    "ref",
    "uri",
)


def path_text(value: object) -> str:
    if isinstance(value, dict):
        return _path_text_from_dict(value)
    text = str(value or "").strip()
    if not text or "://" in text:
        return ""
    return text


def _path_text_from_dict(value: dict[str, object]) -> str:
    for key in ("resolved", "display", "raw", "path", "artifact_ref"):
        text = path_text(value.get(key))
        if text:
            return text
    return ""


def _json_object_from_text(text: str) -> dict[str, Any]:
    try:
        value = json.loads(str(text or ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
