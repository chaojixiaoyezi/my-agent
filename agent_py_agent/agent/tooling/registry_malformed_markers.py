
from __future__ import annotations

from typing import Any

from .registry_markers import next_tool_block_end
from .registry_payload_normalize import parse_error_payload

_MALFORMED_OPENERS = ("[TOOL_CALL",)
_VALID_OPENERS = ("[TOOL_CALL]",)


def malformed_tool_marker_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    calls = [
        (
            pos,
            parse_error_payload(
                "工具调用开始标记格式错误，缺少 ]",
                _malformed_marker_raw(text, pos),
                error_code="TOOL_CALL_MARKER_MALFORMED",
            ),
        )
        for opener in _MALFORMED_OPENERS
        for pos in _malformed_opener_positions(text, opener)
    ]
    return sorted(calls, key=lambda item: item[0])


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


def _is_malformed_protocol_opener(text: str, pos: int, opener: str) -> bool:
    return (
        not _is_valid_opener(text, pos)
        and _looks_like_line_start_marker(text, pos, opener)
    )


def _is_valid_opener(text: str, pos: int) -> bool:
    return any(text.startswith(opener, pos) for opener in _VALID_OPENERS)


def _looks_like_line_start_marker(text: str, pos: int, opener: str) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    if text[line_start:pos].strip():
        return False
    next_char = text[pos + len(opener) : pos + len(opener) + 1]
    return not next_char or next_char.isspace() or next_char in {"{", ":"}


def _malformed_marker_raw(text: str, pos: int) -> str:
    end_info = next_tool_block_end(text, pos)
    if end_info is None:
        return text[pos:].strip()
    end, marker = end_info
    return text[pos : end + len(marker)].strip()
