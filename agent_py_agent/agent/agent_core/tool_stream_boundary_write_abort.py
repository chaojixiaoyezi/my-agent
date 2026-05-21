# LLM: Write-abort helpers keep partial JSON recovery logic out of the main stream-boundary module.
# 模块用途: 负责未闭合 write 工具调用的早停检测、字段提取和恢复 payload 构造。

from __future__ import annotations

import json
import re
from hashlib import sha256

from ..tooling.content_transport_policy import streaming_inline_write_abort_limit
from ..tooling.file_write_session_models import DEFAULT_MAX_SESSION_CHUNK_CHARS
from .tool_stream_boundary_models import (
    LongToolContentAbortPayload,
    LongToolContentStreamAbort,
)

_WRITE_TOOL_NAMES = {"write_file", "append_file", "file_write_session", "write_structured_json"}
_STRUCTURED_JSON_TOOL = "write_structured_json"
_JSON_TOOL_RE = re.compile(
    r'"tool"\s*:\s*"(?P<tool>write_file|append_file|file_write_session|write_structured_json)"'
)
_JSON_ACTION_RE = re.compile(r'"action"\s*:\s*"(?P<action>(?:\\.|[^"\\]){0,32})"')
_JSON_PATH_RE = re.compile(r'"(?:path|target_path)"\s*:\s*"(?P<path>(?:\\.|[^"\\]){0,240})"')
_JSON_SESSION_RE = re.compile(r'"session_id"\s*:\s*"(?P<session_id>(?:\\.|[^"\\]){0,80})"')
_JSON_CHUNK_INDEX_RE = re.compile(r'"chunk_index"\s*:\s*(?P<chunk_index>\d{1,9})')
_JSON_CONTENT_RE = re.compile(r'"content"\s*:\s*"')
_JSON_STRUCTURED_FIELD_RE = re.compile(r'"(?:data|rows|sheets)"\s*:')


# LLM: long_write_stream_abort detects oversized structured write content while a tool call is still open.
# 函数用途: 根据 TOOL_CALL JSON 字段识别未闭合的大 write_file/append_file，避免等到 provider 超时才失败。
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
    if tool == _STRUCTURED_JSON_TOOL:
        return _long_structured_json_stream_abort(raw, limit)
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
            action=_json_action(raw),
            session_id=_json_session_id(raw),
            chunk_index=_json_chunk_index(raw),
            content_prefix=_streamed_json_string_prefix(
                raw[content_start:], max_chars=limit + 2048
            ),
        )
    )


# LLM: structured JSON writer payloads can be large even without a content string.
# 函数用途: 识别未闭合 write_structured_json 的 data/rows/sheets 大参数流，提前转入可恢复错误。
def _long_structured_json_stream_abort(raw: str, limit: int) -> LongToolContentStreamAbort | None:
    match = _JSON_STRUCTURED_FIELD_RE.search(raw)
    if match is None:
        return None
    payload_chars = max(0, len(raw) - match.end())
    if payload_chars <= limit:
        return None
    return LongToolContentStreamAbort(
        LongToolContentAbortPayload(
            tool=_STRUCTURED_JSON_TOOL,
            path=_json_path(raw),
            chars=payload_chars,
            limit=limit,
        )
    )


# LLM: file_write_session is the structured large-file channel and gets a larger stream ceiling than write_file.
# 函数用途: 根据工具类型计算未闭合 content 早停阈值；普通写入早停，大文件 session 按 chunk 上限早停。
def _streaming_write_abort_limit(tool: str, max_chars: int | None) -> int:
    limit = streaming_inline_write_abort_limit(max_chars)
    if tool != "file_write_session":
        return limit
    return max(limit, DEFAULT_MAX_SESSION_CHUNK_CHARS)


# LLM: recovered_write_abort_payload converts an abort into the best follow-up tool call.
# 函数用途: 优先把 file_write_session / write_file 的大正文中断转成真实可继续执行的结构化工具调用。
def recovered_write_abort_payload(exc: LongToolContentStreamAbort) -> dict[str, object] | None:
    recovered = _recovered_file_write_session_append(exc)
    if recovered is not None:
        return recovered
    return _recovered_write_file_append(exc)


# LLM: _json_tool extracts the structured tool name from incomplete JSON without parsing the body.
# 函数用途: 从流式片段里取 write_file/append_file 工具名；只看机器字段，不读自然语言描述。
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


# LLM: _recovered_file_write_session_append converts a partial structured write stream into real work.
# 函数用途: 对 file_write_session.append 的未闭合 content 前缀执行一次真实 append，避免大内容反复解析失败。
def _recovered_file_write_session_append(
    exc: LongToolContentStreamAbort,
) -> dict[str, object] | None:
    if exc.tool != "file_write_session" or exc.action != "append":
        return None
    if not exc.session_id or exc.chunk_index is None or not exc.content_prefix:
        return None
    return {
        "tool": "file_write_session",
        "action": "append",
        "session_id": exc.session_id,
        "chunk_index": exc.chunk_index,
        "content": exc.content_prefix,
    }


# LLM: _recovered_write_file_append preserves the first streamed write_file bytes as a session chunk.
# 函数用途: write_file 大正文未闭合时，把已生成前缀转成可恢复 file_write_session append。
def _recovered_write_file_append(exc: LongToolContentStreamAbort) -> dict[str, object] | None:
    if exc.tool != "write_file" or not exc.path or not exc.content_prefix:
        return None
    return {
        "tool": "file_write_session",
        "action": "append",
        "session_id": _stream_write_session_id(exc.path),
        "target_path": exc.path,
        "chunk_index": 0,
        "content": exc.content_prefix,
    }


# LLM: _stream_write_session_id derives a stable safe id from the target path.
# 函数用途: 让同一路径的流式 write_file 恢复到同一 session，避免重复新建临时目录。
def _stream_write_session_id(path: str) -> str:
    digest = sha256(path.encode("utf-8")).hexdigest()[:12]
    return f"stream_write_{digest}"


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


# LLM: _json_session gives file_write_session aborts a stable short identity when no path is present.
# 函数用途: 从未闭合 JSON 里提取 session_id，避免恢复提示只显示 `<unknown>`。
def _json_session(raw: str) -> str:
    match = _JSON_SESSION_RE.search(raw)
    if match is None:
        return ""
    try:
        session_id = json.loads(f'"{match.group("session_id")}"')
    except json.JSONDecodeError:
        session_id = match.group("session_id")
    return f"session_id={session_id}" if session_id else ""


# LLM: _json_action decodes the file_write_session action from an incomplete machine payload.
# 函数用途: 只读取 action 结构化字段；不能识别时返回空字符串。
def _json_action(raw: str) -> str:
    match = _JSON_ACTION_RE.search(raw)
    if match is None:
        return ""
    try:
        return str(json.loads(f'"{match.group("action")}"'))
    except json.JSONDecodeError:
        return match.group("action")


# LLM: _json_session_id extracts the raw session id without the display prefix.
# 函数用途: 给恢复 append 构造真实工具参数；失败时返回空字符串。
def _json_session_id(raw: str) -> str:
    match = _JSON_SESSION_RE.search(raw)
    if match is None:
        return ""
    try:
        return str(json.loads(f'"{match.group("session_id")}"'))
    except json.JSONDecodeError:
        return match.group("session_id")


# LLM: _json_chunk_index extracts chunk index from incomplete machine JSON.
# 函数用途: 给恢复 append 构造真实 chunk_index；没有结构化字段时返回 None。
def _json_chunk_index(raw: str) -> int | None:
    match = _JSON_CHUNK_INDEX_RE.search(raw)
    if match is None:
        return None
    try:
        return int(match.group("chunk_index"))
    except ValueError:
        return None


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
