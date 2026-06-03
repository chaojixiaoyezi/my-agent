
from __future__ import annotations

_PROTECTED_MARKER_PAIRS = (
    ("[SUBAGENT_RESULT]", "[/SUBAGENT_RESULT]"),
    ("[PARENT_PLANNER_RESULT]", "[/PARENT_PLANNER_RESULT]"),
)


def mask_protected_control_ranges(text: str) -> str:
    ranges = _protected_control_ranges(text)
    if not ranges:
        return text
    chars = list(text)
    for start, end in ranges:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _protected_control_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for start_marker, end_marker in _PROTECTED_MARKER_PAIRS:
        ranges.extend(_marker_ranges(text, start_marker, end_marker))
    return sorted(ranges)


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
