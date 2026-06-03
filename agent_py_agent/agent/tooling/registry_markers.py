
from __future__ import annotations

"""Legacy tool block marker helpers."""


def next_tool_block_start(text: str, cursor: int) -> tuple[int, str] | None:
    start_markers = ["[TOOL_CALL]", "[SUBAGENT_CALL]"]
    starts = [
        (pos, marker)
        for marker in start_markers
        for pos in [text.find(marker, cursor)]
        if pos != -1
    ]
    return min(starts, key=lambda item: item[0]) if starts else None


def next_tool_block_end(text: str, start_at: int) -> tuple[int, str] | None:
    end_markers = ["[/TOOL_CALL]", "[/SUBAGENT_CALL]"]
    ends = [
        (pos, marker)
        for marker in end_markers
        for pos in [text.find(marker, start_at)]
        if pos != -1
    ]
    return min(ends, key=lambda item: item[0]) if ends else None
