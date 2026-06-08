
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
        repaired = _load_json_with_invalid_string_escape_repair(raw)
        if repaired is not None:
            return repaired
        raise exc


def _load_json_with_trailing_brace_repair(raw: str) -> Any | None:
    try:
        payload, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return None
    tail = raw[end:].strip()
    if tail and set(tail) <= {"}", "]"}:
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


def _load_json_with_invalid_string_escape_repair(raw: str) -> Any | None:
    repaired = _escape_invalid_json_string_backslashes(raw)
    if repaired == raw:
        return None
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        repaired_payload = _load_json_with_trailing_brace_repair(repaired)
        if repaired_payload is not None:
            return repaired_payload
        return _load_json_with_detached_top_level_fields(repaired)


def _escape_invalid_json_string_backslashes(raw: str) -> str:
    valid_simple_escapes = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}
    chars: list[str] = []
    in_string = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == '"':
            in_string = not in_string
            chars.append(char)
            index += 1
            continue
        if in_string and char == "\\":
            next_char = raw[index + 1] if index + 1 < len(raw) else ""
            if next_char and next_char in valid_simple_escapes:
                chars.append(char)
                chars.append(next_char)
                index += 2
                continue
            chars.append("\\\\")
            if next_char:
                chars.append(next_char)
                index += 2
            else:
                index += 1
            continue
        chars.append(char)
        index += 1
    return "".join(chars)
