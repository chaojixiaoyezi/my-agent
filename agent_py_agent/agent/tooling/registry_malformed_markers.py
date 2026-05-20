# LLM: Malformed marker detection keeps obvious tool intents from becoming final answers.
# 模块用途: 识别模型写坏的 [TOOL_CALL 开始标记，并生成统一 parse-error 工具载荷。

from __future__ import annotations

from typing import Any

from .registry_markers import next_tool_block_end
from .registry_payload_normalize import parse_error_payload

_MALFORMED_OPENERS = ("[TOOL_CALL", "[SUBAGENT_CALL")
_VALID_OPENERS = ("[TOOL_CALL]", "[SUBAGENT_CALL]")


# LLM: malformed_tool_marker_calls detects protocol-shaped markers, not ordinary task prose.
# 函数用途: 当模型明显想调用工具但少写 `]` 时，返回可执行的 parse-error 记录触发下一轮修复。
def malformed_tool_marker_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    calls = [
        (
            pos,
            parse_error_payload(
                "工具调用开始标记格式错误，缺少 ]",
                _malformed_marker_raw(text, pos),
            ),
        )
        for opener in _MALFORMED_OPENERS
        for pos in _malformed_opener_positions(text, opener)
    ]
    return sorted(calls, key=lambda item: item[0])


# LLM: _malformed_opener_positions isolates marker scanning so callers stay flat.
# 函数用途: 遍历某个坏开头标记的位置，只返回像工具协议而不是普通说明文字的命中。
def _malformed_opener_positions(text: str, opener: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        pos = text.find(opener, cursor)
        if pos == -1:
            return positions
        cursor = pos + len(opener)
        if _is_malformed_protocol_opener(text, pos, opener):
            positions.append(pos)


# LLM: _is_malformed_protocol_opener keeps prose filtering out of the scanner loop.
# 函数用途: 判断一个位置是否为真实坏工具协议开头。
def _is_malformed_protocol_opener(text: str, pos: int, opener: str) -> bool:
    return (
        not _is_valid_opener(text, pos)
        and _looks_like_line_start_marker(text, pos, opener)
    )


# LLM: _is_valid_opener avoids reporting the normal parser's supported markers as malformed.
# 函数用途: 区分合法 `[TOOL_CALL]` 和少写右中括号的坏标记。
def _is_valid_opener(text: str, pos: int) -> bool:
    return any(text.startswith(opener, pos) for opener in _VALID_OPENERS)


# LLM: _looks_like_line_start_marker filters prose mentions such as "use [TOOL_CALL in docs".
# 函数用途: 只把行首工具协议形状当工具意图，避免普通说明文字误触发工具重试。
def _looks_like_line_start_marker(text: str, pos: int, opener: str) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    if text[line_start:pos].strip():
        return False
    next_char = text[pos + len(opener) : pos + len(opener) + 1]
    return not next_char or next_char.isspace() or next_char in {"{", ":"}


# LLM: _malformed_marker_raw bounds the bad protocol sample included in parse diagnostics.
# 函数用途: 提取坏工具块的原文片段，优先到结束标记，否则取剩余输出。
def _malformed_marker_raw(text: str, pos: int) -> str:
    end_info = next_tool_block_end(text, pos)
    if end_info is None:
        return text[pos:].strip()
    end, marker = end_info
    return text[pos : end + len(marker)].strip()
