# LLM: Tool parser control-range helpers isolate structured result blocks from executable tool parsing.
# 模块用途: 识别并屏蔽模型结构化结果块，避免结果摘要里的协议标记被误当作真实工具调用。

from __future__ import annotations

_PROTECTED_MARKER_PAIRS = (
    ("[SUBAGENT_RESULT]", "[/SUBAGENT_RESULT]"),
    ("[PARENT_PLANNER_RESULT]", "[/PARENT_PLANNER_RESULT]"),
)


# LLM: mask_protected_control_ranges preserves text length while hiding inert result payloads.
# 函数用途: 把结构化结果块内部替换为空格；外部工具调用的位置和内容保持可解析。
def mask_protected_control_ranges(text: str) -> str:
    ranges = _protected_control_ranges(text)
    if not ranges:
        return text
    chars = list(text)
    for start, end in ranges:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


# LLM: _protected_control_ranges treats result payloads as data, even when they mention tool markers.
# 函数用途: 找出所有受保护结果块；未闭合块屏蔽到末尾，留给结果解析/修复流程处理。
def _protected_control_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for start_marker, end_marker in _PROTECTED_MARKER_PAIRS:
        ranges.extend(_marker_ranges(text, start_marker, end_marker))
    return sorted(ranges)


# LLM: _marker_ranges is literal and non-recursive because model protocol blocks are flat text.
# 函数用途: 按起止标记提取范围；找不到结束标记时把剩余文本视为受保护结果候选。
def _marker_ranges(text: str, start_marker: str, end_marker: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(start_marker, cursor)
        if start == -1:
            return ranges
        body_start = start + len(start_marker)
        end = text.find(end_marker, body_start)
        if end == -1:
            ranges.append((start, len(text)))
            return ranges
        ranges.append((start, end + len(end_marker)))
        cursor = end + len(end_marker)
