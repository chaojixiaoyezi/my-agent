# LLM: Marker scanning helpers for legacy registry tool blocks.
# 模块用途: 查找 [TOOL_CALL]/[SUBAGENT_CALL] 旧协议块的开始和结束位置。

from __future__ import annotations

"""Legacy tool block marker helpers."""


# LLM: next_tool_block_start finds the earliest supported legacy tool marker.
# 函数用途: 从 cursor 开始查找最近的工具调用开始标记。
def next_tool_block_start(text: str, cursor: int) -> tuple[int, str] | None:
    start_markers = ["[TOOL_CALL]", "[SUBAGENT_CALL]"]
    starts = [
        (pos, marker)
        for marker in start_markers
        for pos in [text.find(marker, cursor)]
        if pos != -1
    ]
    return min(starts, key=lambda item: item[0]) if starts else None


# LLM: next_tool_block_end finds the earliest supported legacy closing marker.
# 函数用途: 从指定位置开始查找最近的工具调用结束标记。
def next_tool_block_end(text: str, start_at: int) -> tuple[int, str] | None:
    end_markers = ["[/TOOL_CALL]", "[/SUBAGENT_CALL]"]
    ends = [
        (pos, marker)
        for marker in end_markers
        for pos in [text.find(marker, start_at)]
        if pos != -1
    ]
    return min(ends, key=lambda item: item[0]) if ends else None
