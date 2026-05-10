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
