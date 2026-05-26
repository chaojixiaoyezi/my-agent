# LLM: 解析容错直接影响工具调用召回，放宽规则时要防误解析。
# 模块用途: 从模型输出中解析 XMLish 工具调用和参数。

from __future__ import annotations

"""parses non-JSON XML-ish tool-call dialects into canonical tool payloads.

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
_XMLISH_BARE_FUNCTION_RE = re.compile(
    r"<function(?:\s*=\s*['\"]?(?P<name1>[^'\">\s]+)['\"]?|\s+name\s*=\s*['\"](?P<name2>[^'\"]+)['\"])\s*>(?P<body>.*?)</function\s*>",
    re.IGNORECASE | re.DOTALL,
)
_XMLISH_BARE_FUNCTION_OPEN_RE = re.compile(
    r"<function(?:\s*=\s*['\"]?(?P<name1>[^'\">\s]+)['\"]?|\s+name\s*=\s*['\"](?P<name2>[^'\"]+)['\"])\s*>",
    re.IGNORECASE,
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
    "patch": "apply_patch",
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


# LLM: parse_xmlish_tool_calls 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 parse_xmlish_tool_calls 数据结构。
def parse_xmlish_tool_calls(text: str) -> list[tuple[int, dict[str, Any]]]:

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

    closed_positions: set[int] = set()
    for match in _XMLISH_BARE_FUNCTION_RE.finditer(text):
        closed_positions.add(match.start())
        name = match.group("name1") or match.group("name2")
        body = match.group("body")
        payload: dict[str, Any] = {"tool": _normalize_xmlish_tool_name(name)}
        for param_match in _XMLISH_PARAMETER_EQ_RE.finditer(body):
            pname = _normalize_xmlish_parameter_name(param_match.group("name"))
            payload[pname] = _decode_xmlish_parameter_value(param_match.group("value"))
        for param_match in _XMLISH_PARAMETER_NAME_RE.finditer(body):
            pname = _normalize_xmlish_parameter_name(param_match.group("name"))
            payload[pname] = _decode_xmlish_parameter_value(param_match.group("value"))
        calls.append((match.start(), payload))

    _handle_bare_function_opens_without_close(text, calls, closed_positions)

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



# LLM: _handle_bare_function_opens_without_close 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 handle_bare_function_opens_without_close 步骤，并保持调用方依赖的数据形状。
def _handle_bare_function_opens_without_close(
    text: str,
    calls: list[tuple[int, dict[str, Any]]],
    closed_positions: set[int],
) -> None:

    opens = list(_XMLISH_BARE_FUNCTION_OPEN_RE.finditer(text))
    for i, match in enumerate(opens):
        if match.start() in closed_positions:
            continue
        calls.append(_bare_function_payload(text, opens, i, match))

    _replace_unclosed_last_bare_function(text, calls, opens, closed_positions)


# LLM: _bare_function_payload 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 bare_function_payload 步骤，并保持调用方依赖的数据形状。
def _bare_function_payload(text: str, opens: list[re.Match[str]], index: int, match: re.Match[str]) -> tuple[int, dict[str, Any]]:
    name = match.group("name1") or match.group("name2")
    next_open = opens[index + 1].start() if index + 1 < len(opens) else len(text)
    body = text[match.end():next_open]
    if not body.strip():
        return (
            match.start(),
            {
                "tool": "__parse_error__",
                "error": "XML-ish bare function call has empty body",
                "raw": _truncate_raw(text[match.start():next_open].strip()),
            },
        )
    return match.start(), _xmlish_payload_from_body(name, body)


# LLM: _xmlish_payload_from_body 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 xmlish_payload_from_body 步骤，并保持调用方依赖的数据形状。
def _xmlish_payload_from_body(name: str, body: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool": _normalize_xmlish_tool_name(name)}
    for param_match in _XMLISH_PARAMETER_EQ_RE.finditer(body):
        pname = _normalize_xmlish_parameter_name(param_match.group("name"))
        payload[pname] = _decode_xmlish_parameter_value(param_match.group("value"))
    for param_match in _XMLISH_PARAMETER_NAME_RE.finditer(body):
        pname = _normalize_xmlish_parameter_name(param_match.group("name"))
        payload[pname] = _decode_xmlish_parameter_value(param_match.group("value"))
    return payload


# LLM: _replace_unclosed_last_bare_function 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 replace_unclosed_last_bare_function 步骤，并保持调用方依赖的数据形状。
def _replace_unclosed_last_bare_function(
    text: str,
    calls: list[tuple[int, dict[str, Any]]],
    opens: list[re.Match[str]],
    closed_positions: set[int],
) -> None:
    if not opens:
        return
    last = opens[-1]
    if last.start() in closed_positions or "</function" in text[last.end():].lower():
        return
    calls[:] = [(pos, pl) for pos, pl in calls if pos != last.start()]
    calls.append(
        (
            last.start(),
            {
                "tool": "__parse_error__",
                "error": "XML-ish bare function call is missing a closing tag",
                "raw": _truncate_raw(text[last.start():].strip()),
            },
        )
    )


# LLM: _parse_xmlish_tool_call_body 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 parse_xmlish_tool_call_body 数据结构。
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


# LLM: _normalize_xmlish_tool_name 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
def _normalize_xmlish_tool_name(name: str) -> str:
    cleaned = name.strip().lower().replace("-", "_")
    return _XMLISH_TOOL_ALIASES.get(cleaned, cleaned)


# LLM: _normalize_xmlish_parameter_name 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
def _normalize_xmlish_parameter_name(name: str) -> str:
    cleaned = name.strip().lower().replace("-", "_")
    return _XMLISH_PARAMETER_ALIASES.get(cleaned, cleaned)


# LLM: _decode_xmlish_parameter_value 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 decode_xmlish_parameter_value 步骤，并保持调用方依赖的数据形状。
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


# LLM: _truncate_raw 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 truncate_raw 步骤，并保持调用方依赖的数据形状。
def _truncate_raw(text: str) -> str:
    if len(text) <= _MAX_XMLISH_RAW_CHARS:
        return text
    return text[:_MAX_XMLISH_RAW_CHARS] + "\n... 已截断"
