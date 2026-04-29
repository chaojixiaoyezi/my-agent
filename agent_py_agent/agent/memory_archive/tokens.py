from __future__ import annotations

"""LLM: conservative token estimation helpers for memory budgeting.

给人看的解释：
这里不是精确 tokenizer，也不假装精确。
它只给压缩前 hook、状态面板和预算提示一个偏保守的估算，避免中文、英文和工具大结果被严重低估。
"""

import json
import math
from typing import Any


def estimate_tokens(payload: Any) -> int:
    """LLM: estimate token count conservatively without provider-specific tokenizers.

    给人看的解释：
    真正 token 数要看模型 tokenizer。
    这里用字符、UTF-8 字节、中文字符和英文片段一起估算，宁愿稍微多算一点，也不要把上下文快满这件事看轻。
    """

    text = _payload_to_text(payload)
    if not text:
        return 1

    cjk_chars = sum(1 for char in text if _is_cjk(char))
    non_cjk_chars = len(text) - cjk_chars
    utf8_bytes = len(text.encode("utf-8"))
    structured_overhead = _structured_overhead(payload)

    cjk_estimate = cjk_chars + math.ceil(non_cjk_chars / 4)
    byte_estimate = math.ceil(utf8_bytes / 3)
    dense_text_estimate = math.ceil(len(text) / 3)

    return max(1, cjk_estimate, byte_estimate, dense_text_estimate) + structured_overhead


def _payload_to_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _structured_overhead(payload: Any) -> int:
    if isinstance(payload, dict):
        return max(1, len(payload) // 2)
    if isinstance(payload, list | tuple | set):
        return max(1, len(payload) // 4)
    return 0


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )
