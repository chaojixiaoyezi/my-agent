# LLM: JSON repair helpers keep model tool-call drift handling out of the registry executor.
# 模块用途: 提供极窄的工具调用 JSON 解析修复，不吞掉多个对象或任意坏格式。

from __future__ import annotations

import json
from typing import Any


# LLM: load_tool_block_json tolerates one real-model trailing-brace slip without broad repair.
# 函数用途: 解析工具块 JSON；如果有效对象后只多出右花括号，则保守取第一个对象。
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


# LLM: _load_json_with_trailing_brace_repair is a narrow recovery for MiniMax-style extra `}`.
# 函数用途: 只在第一个 JSON 对象后剩余内容全是右花括号时修复，避免吞掉第二个工具对象。
def _load_json_with_trailing_brace_repair(raw: str) -> Any | None:
    try:
        payload, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return None
    tail = raw[end:].strip()
    if tail and set(tail) <= {"}"}:
        return payload
    return None


# LLM: _load_json_with_detached_top_level_fields repairs premature object close before sibling fields.
# 函数用途: 模型把 source_refs/claims 等顶层参数误放到多余 `}` 后面时，按 JSON 结构合回同一工具调用。
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


# LLM: _load_write_file_json_with_trailing_body bridges common raw-body file-write drift into one canonical payload.
# 函数用途: 当模型先输出写文件 JSON 头、再把正文直接跟在后面时，把尾部内容并回 content 字段。
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
