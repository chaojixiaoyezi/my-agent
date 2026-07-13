
from __future__ import annotations

import json
import re
from typing import Any

_ARROW_CLI_TOOL_CALL_RE = re.compile(
    r'^\s*\{\s*tool\s*=>\s*(?P<tool>"(?:\\.|[^"\\])*")\s*,?\s*'
    r'args\s*=>\s*\{(?P<args>.*?)\}\s*\}\s*$',
    re.DOTALL,
)
_ARROW_CLI_ARGUMENT_RE = re.compile(
    r"^--(?P<name>[A-Za-z_][A-Za-z0-9_-]{0,127})\s+(?P<value>.+)$"
)


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
        repaired = _load_arrow_cli_tool_call(raw)
        if repaired is not None:
            return repaired
        raise exc


def _load_arrow_cli_tool_call(raw: str) -> dict[str, Any] | None:
    """Repair one bounded non-JSON tool-call dialect emitted by some models.

    MiniMax may fall back from native ``tool_use`` to a textual block shaped as::

        {tool => "session_search", args => {
          --query "old topic"
          --window 10
        }}

    This is parsed deliberately more narrowly than a shell command language.  Every
    value must be a complete JSON scalar/container on one line, argument names are
    bounded identifiers, duplicates are rejected, and no trailing prose is allowed.
    The resulting ordinary payload still passes through the registry's schema,
    authorization, path and runtime gates before any tool can execute.
    """

    matched = _ARROW_CLI_TOOL_CALL_RE.fullmatch(raw)
    if matched is None:
        return None
    try:
        tool = json.loads(matched.group("tool"))
    except json.JSONDecodeError:
        return None
    if not isinstance(tool, str) or not tool.strip():
        return None

    payload: dict[str, Any] = {"tool": tool}
    arguments = matched.group("args").strip()
    if not arguments:
        return payload
    for raw_line in arguments.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        argument = _ARROW_CLI_ARGUMENT_RE.fullmatch(line)
        if argument is None:
            return None
        name = argument.group("name").replace("-", "_")
        if name == "tool" or name in payload:
            return None
        try:
            value = json.loads(argument.group("value"))
        except json.JSONDecodeError:
            return None
        payload[name] = value
    return payload


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
            index = _append_repaired_escape(raw, index, chars, valid_simple_escapes)
            continue
        chars.append(char)
        index += 1
    return "".join(chars)


def _append_repaired_escape(raw: str, index: int, chars: list[str], valid_simple_escapes: set[str]) -> int:
    next_char = raw[index + 1] if index + 1 < len(raw) else ""
    if next_char and next_char in valid_simple_escapes:
        chars.append(raw[index])
        chars.append(next_char)
        return index + 2
    chars.append("\\\\")
    if next_char:
        chars.append(next_char)
        return index + 2
    return index + 1
