
from __future__ import annotations

import json
from typing import Any


def load_tool_block_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        repaired = _load_json_with_trailing_brace_repair(raw)
        if repaired is not None:
            return repaired
        repaired = _load_json_with_detached_top_level_fields(raw)
        if repaired is not None:
            return repaired
        repaired = _load_write_file_json_with_trailing_body(raw)
        if repaired is not None:
            return repaired
        raise exc


def _load_json_with_trailing_brace_repair(raw: str) -> Any | None:
    try:
        payload, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return None
    tail = raw[end:].strip()
    if tail and set(tail) <= {"}"}:
        return payload
    return None


def _load_json_with_detached_top_level_fields(raw: str) -> Any | None:
    try:
        payload, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    tail = raw[end:].strip()
    if not tail.startswith(","):
        return None
    candidate = "{" + tail.lstrip(",").strip()
    try:
        extra = json.loads(candidate)
    except json.JSONDecodeError:
        extra = _load_json_with_trailing_brace_repair(candidate)
    if extra is None:
        return None
    if not isinstance(extra, dict) or "tool" in extra:
        return None
    if set(payload).intersection(extra):
        return None
    return {**payload, **extra}


def _load_write_file_json_with_trailing_body(raw: str) -> Any | None:
    try:
        payload, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    tool = str(payload.get("tool") or "").strip().lower()
    if tool not in {"write_file", "write_file_raw", "write"}:
        return None
    if str(payload.get("content") or "").strip():
        return None
    tail = raw[end:].lstrip("\r\n")
    if not tail.strip():
        return None
    return {**payload, "content": tail}
