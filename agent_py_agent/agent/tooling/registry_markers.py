
from __future__ import annotations

"""Tool block marker helpers."""


def next_tool_block_start(text: str, cursor: int) -> tuple[int, str] | None:
    start_markers = ["[TOOL_CALL]"]
    starts = [
        (pos, marker)
        for marker in start_markers
        for pos in [_next_protocol_marker_pos(text, marker, cursor)]
        if pos != -1
    ]
    return min(starts, key=lambda item: item[0]) if starts else None


def next_tool_block_end(text: str, start_at: int) -> tuple[int, str] | None:
    end_markers = ["[/TOOL_CALL]"]
    ends = [
        (pos, marker)
        for marker in end_markers
        for pos in [_next_protocol_marker_pos(text, marker, start_at)]
        if pos != -1
    ]
    return min(ends, key=lambda item: item[0]) if ends else None


def _next_protocol_marker_pos(text: str, marker: str, cursor: int) -> int:
    while True:
        pos = text.find(marker, cursor)
        if pos == -1:
            return -1
        if _marker_starts_protocol_line(text, pos):
            return pos
        cursor = pos + len(marker)


def _marker_starts_protocol_line(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return not text[line_start:pos].strip()
