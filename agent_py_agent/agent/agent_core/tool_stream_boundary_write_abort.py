# LLM: Write-abort helpers keep partial JSON recovery logic out of the main stream-boundary module.
# 模块用途: 负责未闭合 write 工具调用的早停检测、字段提取和恢复 payload 构造。

from __future__ import annotations

import json
import re

from ..tooling.content_transport_policy import (
    streaming_inline_write_abort_limit,
)
from .tool_stream_boundary_models import (
    LongToolContentAbortPayload,
    LongToolContentStreamAbort,
)

_WRITE_TOOL_NAMES = {"write_file"}
_JSON_TOOL_RE = re.compile(r'"tool"\s*:\s*"(?P<tool>write_file)"')
_JSON_PATH_RE = re.compile(r'"(?:path|target_path)"\s*:\s*"(?P<path>(?:\\.|[^"\\]){0,240})"')
_JSON_SESSION_RE = re.compile(r'"session_id"\s*:\s*"(?P<session_id>(?:\\.|[^"\\]){0,80})"')
_JSON_CONTENT_RE = re.compile(r'"content"\s*:\s*"')


# LLM: long_write_stream_abort detects oversized structured write content while a tool call is still open.
# 函数用途: 根据 TOOL_CALL JSON 字段识别未闭合的大 write_file，避免等到 provider 超时才失败。
def long_write_stream_abort(
    text: str,
    *,
    max_chars: int | None,
    start_info: tuple[int, str] | None,
    first_end_marker: callable,
) -> LongToolContentStreamAbort | None:
    if start_info is None:
        return None
    start, marker = start_info
    if first_end_marker(start + len(marker)) is not None:
        return None
    raw = text[start + len(marker) :]
    tool = _json_tool(raw)
    if tool not in _WRITE_TOOL_NAMES:
        return None
    limit = _streaming_write_abort_limit(tool, max_chars)
    content_start = _content_value_start(raw)
    if content_start is None:
        return None
    content_chars = _streamed_json_string_chars(raw[content_start:])
    if content_chars <= limit:
        return None
    return LongToolContentStreamAbort(
        LongToolContentAbortPayload(
            tool=tool,
            path=_json_path(raw),
            chars=content_chars,
            limit=limit,
            content_prefix=_streamed_json_string_prefix(
                raw[content_start:], max_chars=limit + 2048
            ),
        )
    )


# LLM: write_file streams get a generous ceiling so normal large files can close cleanly.
# 函数用途: 根据配置计算未闭合 content 早停阈值。
def _streaming_write_abort_limit(_tool: str, max_chars: int | None) -> int:
    return streaming_inline_write_abort_limit(max_chars)


# LLM: streamed write recovery no longer creates hidden file sessions.
# 函数用途: 早停后只让上层给模型重试提示，不自动生成专项写入工具调用。
def recovered_write_abort_payload(exc: LongToolContentStreamAbort) -> dict[str, object] | None:
    return None


# LLM: _json_tool extracts the structured tool name from incomplete JSON without parsing the body.
# 函数用途: 从流式片段里取 write_file 工具名；只看机器字段，不读自然语言描述。
def _json_tool(raw: str) -> str:
    match = _JSON_TOOL_RE.search(raw)
    return match.group("tool") if match is not None else ""


# LLM: _content_value_start locates the start of the JSON string value without parsing incomplete JSON.
# 函数用途: 在未闭合工具调用里找到 content 字符串开头；支持顶层和 filesystem 包裹参数。
def _content_value_start(raw: str) -> int | None:
    match = _JSON_CONTENT_RE.search(raw)
    return match.end() if match is not None else None


# LLM: _streamed_json_string_chars counts a partial JSON string value without keeping the full payload.
# 函数用途: 统计流式 content 已输出的字符数；遇到未转义引号说明字符串闭合并停止。
def _streamed_json_string_chars(text: str) -> int:
    count = 0
    escaped = False
    for char in text:
        if escaped:
            count += 1
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
        count += 1
    return count


# LLM: _json_path decodes a bounded path field from an incomplete structured tool payload.
# 函数用途: 从未闭合 JSON 里取 path 字段，失败时返回短原文，避免把大正文带进恢复上下文。
def _json_path(raw: str) -> str:
    match = _JSON_PATH_RE.search(raw)
    if match is None:
        return _json_session(raw)
    try:
        return json.loads(f'"{match.group("path")}"')
    except json.JSONDecodeError:
        return match.group("path")


# LLM: _json_session gives incomplete write payloads a bounded fallback identity when no path is present.
# 函数用途: 从未闭合 JSON 里提取短标识，避免恢复提示只显示 `<unknown>`。
def _json_session(raw: str) -> str:
    match = _JSON_SESSION_RE.search(raw)
    if match is None:
        return ""
    try:
        session_id = json.loads(f'"{match.group("session_id")}"')
    except json.JSONDecodeError:
        session_id = match.group("session_id")
    return f"session_id={session_id}" if session_id else ""

# LLM: _streamed_json_string_prefix decodes a bounded prefix of an incomplete JSON string value.
# 函数用途: 从未闭合 content 字符串中恢复已流出的机器载荷，不读取周围自然语言。
def _streamed_json_string_prefix(text: str, *, max_chars: int) -> str:
    raw_parts: list[str] = []
    escaped = False
    decoded_chars = 0
    for char in text:
        if decoded_chars >= max_chars:
            break
        if escaped:
            raw_parts.append("\\" + char)
            escaped = False
            decoded_chars += 1
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
        raw_parts.append(char)
        decoded_chars += 1
    raw_prefix = "".join(raw_parts)
    try:
        return str(json.loads(f'"{raw_prefix}"'))
    except json.JSONDecodeError:
        return raw_prefix.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')


__all__ = [
    "long_write_stream_abort",
    "recovered_write_abort_payload",
]
