from __future__ import annotations

"""LLM: parses non-JSON XML-ish tool-call dialects into canonical tool payloads.

给人看的解释：
有些模型不会严格输出 `[TOOL_CALL]{json}[/TOOL_CALL]`，而是吐出像 XML 的格式。
这个文件专门把那种格式翻译成统一的工具调用字典。
翻译失败时也不会让主流程崩掉，而是返回一个可处理的 parse error。
"""

import html
import json
import re
from typing import Any



_XMLISH_TOOL_BLOCK_RE = re.compile(
    r"<tool_call\b[^>]*>(?P<body>.*?)</tool_call\s*>",
    re.IGNORECASE | re.DOTALL,
)
_XMLISH_FUNCTION_EQ_RE = re.compile(
    r"<function\s*=\s*['\"]?(?P<name>[^'\">\s]+)['\"]?\s*>",
    re.IGNORECASE,
)
_XMLISH_FUNCTION_NAME_RE = re.compile(
    r"<function\b[^>]*\bname\s*=\s*['\"](?P<name>[^'\"]+)['\"][^>]*>",
    re.IGNORECASE,
)
_XMLISH_PARAMETER_EQ_RE = re.compile(
    r"<parameter\s*=\s*['\"]?(?P<name>[^'\">\s]+)['\"]?\s*>(?P<value>.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
_XMLISH_PARAMETER_NAME_RE = re.compile(
    r"<parameter\b[^>]*\bname\s*=\s*['\"](?P<name>[^'\"]+)['\"][^>]*>(?P<value>.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)

_XMLISH_TOOL_ALIASES = {
    "append": "append_file",
    "append_file": "append_file",
    "cat": "read_file",
    "fetch": "fetch_url",
    "fetch_url": "fetch_url",
    "grep": "search_text",
    "http": "http_request",
    "http_request": "http_request",
    "list": "list_files",
    "list_files": "list_files",
    "ls": "list_files",
    "open": "read_file",
    "read": "read_file",
    "read_file": "read_file",
    "replace": "replace_in_file",
    "replace_in_file": "replace_in_file",
    "request": "http_request",
    "search": "search_text",
    "search_text": "search_text",
    "write": "write_file",
    "write_file": "write_file",
}

_XMLISH_PARAMETER_ALIASES = {
    "file": "path",
    "file_path": "path",
    "filepath": "path",
    "filename": "path",
}
_MAX_XMLISH_RAW_CHARS = 1000


def parse_xmlish_tool_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    """Parse Qwen/通道运行时 XML-ish tool calls.

    Some runtimes emit blocks like:
    <tool_call><function=read><parameter=file_path>README.md</parameter>...

    They are not real XML, so we parse this small dialect explicitly. Broken
    blocks become __parse_error__ payloads instead of crashing the agent loop.
    """

    calls: list[tuple[int, dict[str, Any]]] = []
    cursor = 0
    for match in _XMLISH_TOOL_BLOCK_RE.finditer(text):
        calls.append(
            (
                match.start(),
                _parse_xmlish_tool_call_body(match.group("body"), match.group(0)),
            )
        )
        cursor = match.end()

    tail_start = text.lower().find("<tool_call", cursor)
    if tail_start != -1:
        calls.append(
            (
                tail_start,
                {
                    "tool": "__parse_error__",
                    "error": "XML-ish tool call is missing a closing </tool_call> tag",
                    "raw": _truncate_raw(text[tail_start:].strip()),
                },
            )
        )
    return calls


def _parse_xmlish_tool_call_body(body: str, raw: str) -> dict[str, Any]:
    function_match = _XMLISH_FUNCTION_EQ_RE.search(body)
    if function_match is None:
        function_match = _XMLISH_FUNCTION_NAME_RE.search(body)
    if function_match is None:
        return {
            "tool": "__parse_error__",
            "error": "XML-ish tool call is missing a function name",
            "raw": _truncate_raw(raw.strip()),
        }

    payload: dict[str, Any] = {
        "tool": _normalize_xmlish_tool_name(function_match.group("name"))
    }
    parameter_matches = list(_XMLISH_PARAMETER_EQ_RE.finditer(body))
    parameter_matches.extend(_XMLISH_PARAMETER_NAME_RE.finditer(body))
    parameter_matches.sort(key=lambda item: item.start())
    for parameter_match in parameter_matches:
        name = _normalize_xmlish_parameter_name(parameter_match.group("name"))
        payload[name] = _decode_xmlish_parameter_value(parameter_match.group("value"))
    return payload


def _normalize_xmlish_tool_name(name: str) -> str:
    cleaned = name.strip().lower().replace("-", "_")
    return _XMLISH_TOOL_ALIASES.get(cleaned, cleaned)


def _normalize_xmlish_parameter_name(name: str) -> str:
    cleaned = name.strip().lower().replace("-", "_")
    return _XMLISH_PARAMETER_ALIASES.get(cleaned, cleaned)


def _decode_xmlish_parameter_value(value: str) -> Any:
    text = html.unescape(value.strip())
    if not text:
        return ""
    lower = text.lower()
    looks_like_json = (
        text[0] in '{"['
        or lower in {"true", "false", "null"}
        or re.fullmatch(r"-?\d+(?:\.\d+)?", text) is not None
    )
    if looks_like_json:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _truncate_raw(text: str) -> str:
    if len(text) <= _MAX_XMLISH_RAW_CHARS:
        return text
    return text[:_MAX_XMLISH_RAW_CHARS] + "\n... 已截断"
