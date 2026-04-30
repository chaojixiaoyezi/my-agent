from __future__ import annotations

"""LLM: conservative token estimation helpers for memory budgeting.

新手说明:
这里不是精确 tokenizer，也不假装精确。
它只给压缩前 hook、状态面板和预算提示一个偏保守的估算，避免中文、英文和工具大结果被严重低估。
"""

import json
import math
from typing import Any


def estimate_tokens(payload: Any) -> int:
    """LLM: estimate token count conservatively without provider-specific tokenizers.

    新手说明:
    真正 token 数要看模型 tokenizer。
    这里用字符、UTF-8 字节、中文字符和英文片段一起估算，宁愿稍微多算一点，也不要把上下文快满这件事看轻。

    参数说明:
    `payload` 可以是字符串、字典、列表或任何可转成字符串的对象。

    返回说明:
    返回至少为 1 的整数 token 估算值。
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
    """LLM: convert any payload into text before token estimation.

    新手说明:
    估算 token 前先把结构化对象变成 JSON 字符串；如果 JSON 序列化失败，就退回 `str()`。

    参数说明:
    `payload` 是待估算对象。

    返回说明:
    返回可用于字符统计的字符串。
    """

    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _structured_overhead(payload: Any) -> int:
    """LLM: add a small overhead for structured containers.

    新手说明:
    JSON 结构里的字段名、括号和分隔符也会消耗 token。这里按字段/元素数量补一点预算。

    参数说明:
    `payload` 是原始对象。

    返回说明:
    返回额外 token 估算值。
    """

    if isinstance(payload, dict):
        return max(1, len(payload) // 2)
    if isinstance(payload, list | tuple | set):
        return max(1, len(payload) // 4)
    return 0


def _is_cjk(char: str) -> bool:
    """LLM: detect whether one character is in common CJK ranges.

    新手说明:
    中文字符通常不能按英文“四字符一个 token”粗算，所以单独统计。

    参数说明:
    `char` 是单个字符。

    返回说明:
    中文/日文/韩文常见汉字范围内返回 True，否则 False。
    """

    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )
