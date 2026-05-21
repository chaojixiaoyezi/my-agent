# LLM: File-write raw blocks provide structured large-content transport outside JSON strings.
# 模块用途: 解析写文件 raw block，转成 write_file 或 file_write_session.append 工具调用。

from __future__ import annotations

import json
import re
from typing import Any

from .registry_payload_normalize import parse_error_payload

_APPEND_BLOCK_RE = re.compile(
    r"\[FILE_WRITE_SESSION_APPEND(?P<attrs>[^\]]*)\](?P<content>.*?)\[/FILE_WRITE_SESSION_APPEND\]",
    re.DOTALL,
)
_WRITE_FILE_BLOCK_RE = re.compile(
    r"\[WRITE_FILE_RAW(?P<attrs>[^\]]*)\](?P<content>.*?)\[/WRITE_FILE_RAW\]",
    re.DOTALL,
)
_RAW_BLOCK_MARKERS = ("FILE_WRITE_SESSION_APPEND", "WRITE_FILE_RAW")
_ATTR_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*"
    r"(?:\"(?P<double>(?:\\.|[^\"\\])*)\"|'(?P<single>(?:\\.|[^'\\])*)'|(?P<bare>[^\s\]]+))"
)


# LLM: parse_file_write_session_raw_blocks reads only explicit machine markers and header attributes.
# 函数用途: 把大文件 raw block 解析成 file_write_session append 参数；正文不经过自然语言判断。
def parse_file_write_session_raw_blocks(text: str) -> list[tuple[int, dict[str, Any]]]:
    calls: list[tuple[int, dict[str, Any]]] = []
    for match in _APPEND_BLOCK_RE.finditer(text):
        attrs = _parse_attrs(match.group("attrs"))
        error = _attrs_error(attrs)
        if error:
            calls.append((match.start(), parse_error_payload(error, match.group(0))))
            continue
        calls.append((match.start(), _append_payload(attrs, match.group("content"))))
    return calls


# LLM: parse_write_file_raw_blocks is the one-shot structured commit path for complete files.
# 函数用途: 把 [WRITE_FILE_RAW path="..."] 原文块解析成 write_file 调用，避免模型手工续 chunk。
def parse_write_file_raw_blocks(text: str) -> list[tuple[int, dict[str, Any]]]:
    calls: list[tuple[int, dict[str, Any]]] = []
    for match in _WRITE_FILE_BLOCK_RE.finditer(text):
        attrs = _parse_attrs(match.group("attrs"))
        error = _write_attrs_error(attrs)
        if error:
            calls.append((match.start(), parse_error_payload(error, match.group(0))))
            continue
        calls.append((match.start(), _write_file_payload(attrs, match.group("content"))))
    return calls


# LLM: malformed_file_write_raw_block_calls prevents broken write markers from becoming final prose.
# 函数用途: 模型明显开始写文件 raw block 但没按机器协议闭合时，生成 parse-error 触发下一轮修复。
def malformed_file_write_raw_block_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    valid_ranges = _valid_raw_block_ranges(text)
    calls: list[tuple[int, dict[str, Any]]] = []
    for marker in _RAW_BLOCK_MARKERS:
        calls.extend(_malformed_raw_marker_calls(text, marker, valid_ranges))
    return sorted(calls, key=lambda item: item[0])


# LLM: _parse_attrs keeps the block header as a small structured map.
# 函数用途: 解析 session_id、target_path、chunk_index 等 header 属性，不读取正文语义。
def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        value = match.group("double")
        if value is None:
            value = match.group("single")
        if value is None:
            value = match.group("bare") or ""
        attrs[match.group("key")] = _decode_attr_value(value)
    return attrs


# LLM: _decode_attr_value handles JSON-style escapes in quoted header fields.
# 函数用途: 让路径和 session_id 可包含转义字符，同时坏转义保留原文方便诊断。
def _decode_attr_value(value: str) -> str:
    try:
        return str(json.loads(f'"{value}"'))
    except json.JSONDecodeError:
        return value


# LLM: _attrs_error validates required machine fields before the tool layer mutates disk.
# 函数用途: 检查 raw block 是否具备 append 所需字段，缺失时返回结构化 parse error 文案。
def _attrs_error(attrs: dict[str, str]) -> str:
    required = ("session_id", "target_path", "chunk_index")
    missing = [key for key in required if not str(attrs.get(key) or "").strip()]
    if missing:
        return "FILE_WRITE_SESSION_APPEND 缺少结构化属性: " + ", ".join(missing)
    try:
        int(str(attrs["chunk_index"]))
    except ValueError:
        return "FILE_WRITE_SESSION_APPEND chunk_index 必须是整数"
    return ""


# LLM: _write_attrs_error validates WRITE_FILE_RAW headers before write_file mutates disk.
# 函数用途: 检查一次性写文件 raw block 的 path 字段是否存在。
def _write_attrs_error(attrs: dict[str, str]) -> str:
    if not str(attrs.get("path") or "").strip():
        return "WRITE_FILE_RAW 缺少结构化属性: path"
    return ""


# LLM: _append_payload emits the same dict shape as JSON file_write_session.append.
# 函数用途: 构造工具调用 payload，并只去掉 raw block 外围换行，保留正文内容本身。
def _append_payload(attrs: dict[str, str], content: str) -> dict[str, Any]:
    return {
        "tool": "file_write_session",
        "action": "append",
        "session_id": attrs["session_id"],
        "target_path": attrs["target_path"],
        "chunk_index": int(attrs["chunk_index"]),
        "content": _block_content(content),
    }


# LLM: _write_file_payload emits the same dict shape as JSON write_file.
# 函数用途: 构造 write_file payload；正文只做协议换行剥离，不做语义判断。
def _write_file_payload(attrs: dict[str, str], content: str) -> dict[str, Any]:
    return {
        "tool": "write_file",
        "path": attrs["path"],
        "content": _block_content(content),
    }


# LLM: _block_content removes syntax padding while preserving generated file bytes.
# 函数用途: 去掉 marker 后第一行和结束 marker 前一行的协议换行，不改正文内部内容。
def _block_content(content: str) -> str:
    if content.startswith("\n"):
        content = content[1:]
    if content.endswith("\n"):
        content = content[:-1]
    return content


# LLM: _valid_raw_block_ranges lets malformed detection ignore blocks handled by the normal parsers.
# 函数用途: 标记已经完整闭合的 raw block 区间，避免一个坏属性块被重复报两次错误。
def _valid_raw_block_ranges(text: str) -> list[tuple[int, int]]:
    ranges = [(match.start(), match.end()) for match in _APPEND_BLOCK_RE.finditer(text)]
    ranges.extend((match.start(), match.end()) for match in _WRITE_FILE_BLOCK_RE.finditer(text))
    return ranges


# LLM: _position_in_ranges keeps scanner logic independent from regex match internals.
# 函数用途: 判断某个 marker 位置是否已经属于完整 raw block。
def _position_in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


# LLM: _malformed_raw_marker_calls handles one marker type so the public scanner stays flat.
# 函数用途: 返回某种 raw 写文件 marker 的坏协议 parse-error 列表。
def _malformed_raw_marker_calls(
    text: str,
    marker: str,
    valid_ranges: list[tuple[int, int]],
) -> list[tuple[int, dict[str, Any]]]:
    opener = f"[{marker}"
    return [
        (pos, parse_error_payload(_malformed_raw_block_error(marker), _raw_block_sample(text, pos)))
        for pos in _raw_marker_positions(text, opener)
        if not _position_in_ranges(pos, valid_ranges) and _looks_like_raw_block_opener(text, pos, opener)
    ]


# LLM: _raw_marker_positions is a tiny literal scanner for machine marker starts.
# 函数用途: 找到某个 raw marker 的所有出现位置；是否有效由调用方继续判断。
def _raw_marker_positions(text: str, opener: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        pos = text.find(opener, cursor)
        if pos == -1:
            return positions
        positions.append(pos)
        cursor = pos + len(opener)


# LLM: _looks_like_raw_block_opener mirrors tool marker filtering without reading prose as facts.
# 函数用途: 只把行首 raw 写文件协议形状当机器协议，避免文档里的普通说明误触发。
def _looks_like_raw_block_opener(text: str, pos: int, opener: str) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    if text[line_start:pos].strip():
        return False
    next_char = text[pos + len(opener) : pos + len(opener) + 1]
    return not next_char or next_char.isspace() or next_char in {"]", ":"}


# LLM: _malformed_raw_block_error returns stable protocol diagnostics for model recovery.
# 函数用途: 用固定错误码式描述告诉下一轮这不是最终答案，而是坏机器块。
def _malformed_raw_block_error(marker: str) -> str:
    return f"{marker} 原文块格式错误，缺少完整结构化 header 或结束标记 [/{marker}]"


# LLM: _raw_block_sample bounds the broken protocol sample in parse diagnostics.
# 函数用途: 提取坏 raw block 片段，优先在其它机器结束标记处截断，避免污染下一轮上下文。
def _raw_block_sample(text: str, pos: int) -> str:
    candidates = [
        idx + len(marker)
        for marker in ("[/FILE_WRITE_SESSION_APPEND]", "[/WRITE_FILE_RAW]", "[/TOOL_CALL]", "[/SUBAGENT_CALL]")
        if (idx := text.find(marker, pos)) != -1
    ]
    end = min(candidates) if candidates else len(text)
    return text[pos:end].strip()


__all__ = [
    "malformed_file_write_raw_block_calls",
    "parse_file_write_session_raw_blocks",
    "parse_write_file_raw_blocks",
]
