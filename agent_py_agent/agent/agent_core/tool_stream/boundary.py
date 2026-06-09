
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ...backends import ModelResponse
from ...tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
)
from .write_abort import (
    LongToolContentStreamAbort,
    long_write_stream_abort,
    recovered_write_abort_payload,
)

_TOOL_START_MARKERS = ("[TOOL_CALL]",)
_TOOL_END_MARKERS = ("[/TOOL_CALL]",)
_RAW_MACHINE_BLOCK_PATTERNS = (
    re.compile(r"\[WRITE_FILE_RAW[^\]]*\].*?\[/WRITE_FILE_RAW\]", re.DOTALL),
)
_MAX_UNCLOSED_TOOL_START_MARKERS = 1


class MalformedToolProtocolStreamAbort(RuntimeError):
    def __init__(self, *, start_marker: str, marker_count: int, limit: int) -> None:
        super().__init__(f"tool protocol emitted {marker_count} unclosed {start_marker} markers")
        self.start_marker = start_marker
        self.marker_count = marker_count
        self.limit = limit


def first_complete_tool_call_cut_index(text: str) -> int | None:
    start_info = _first_marker(text, _TOOL_START_MARKERS, 0)
    if start_info is None:
        return None
    start, marker = start_info
    end_info = _first_tool_end_marker(text, start + len(marker))
    if end_info is None:
        return None
    end, end_marker = end_info
    return end + len(end_marker)


def cut_response_after_first_complete_tool_call(response: ModelResponse) -> tuple[ModelResponse, bool]:
    machine_text = complete_machine_block_text(response.text)
    if not machine_text:
        return response, False
    if machine_text == response.text.strip():
        return response, False
    return ModelResponse(text=machine_text, backend=response.backend), True


def complete_machine_block_text(text: str) -> str:
    ranges = _complete_machine_block_ranges(text)
    if not ranges:
        return ""
    return "\n".join(text[start:end].strip() for start, end in ranges)


def _complete_machine_block_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = _complete_tool_call_block_ranges(text)
    for pattern in _RAW_MACHINE_BLOCK_PATTERNS:
        ranges.extend((match.start(), match.end()) for match in pattern.finditer(text))
    ranges.sort(key=lambda item: item[0])
    return _non_overlapping_ranges(ranges)


def _complete_tool_call_block_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start_info = _first_marker(text, _TOOL_START_MARKERS, cursor)
        if start_info is None:
            return ranges
        start, start_marker = start_info
        body_start = start + len(start_marker)
        end_info = _first_tool_end_marker(text, body_start)
        if end_info is None:
            return ranges
        end, end_marker = end_info
        cursor = end + len(end_marker)
        ranges.append((start, cursor))


def _non_overlapping_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    kept: list[tuple[int, int]] = []
    last_end = -1
    for start, end in ranges:
        if start < last_end:
            continue
        kept.append((start, end))
        last_end = end
    return kept


@dataclass
class ToolBoundaryChunkFilter:
    on_chunk: Callable[[str], None] | None
    max_inline_content_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS
    _text: str = ""
    _forwarded: int = 0
    _closed: bool = False
    cut_detected: bool = field(default=False, init=False)
    cut_index: int | None = field(default=None, init=False)

    def __call__(self, chunk: str) -> None:
        self._text += str(chunk or "")
        protocol_abort = malformed_tool_protocol_stream_abort(self._text)
        if protocol_abort is not None:
            raise protocol_abort
        start_info = _open_tool_start(self._text)
        abort = long_write_stream_abort(
            self._text,
            max_chars=self.max_inline_content_chars,
            start_info=start_info,
            first_end_marker=lambda cursor: _first_tool_end_marker(self._text, cursor),
        )
        if abort is not None:
            raise abort
        cut_index = first_complete_tool_call_cut_index(self._text)
        if cut_index is None:
            if self.on_chunk is not None:
                self._forward_to(len(self._text))
            return
        if not self.cut_detected:
            self.cut_detected = True
            self.cut_index = cut_index
        if self.on_chunk is not None and not self._closed:
            self._forward_to(cut_index)
        self._closed = True

    def finish(self) -> None:
        if self.on_chunk is None or self._closed:
            return
        if self.cut_detected:
            return
        self._forward_to(len(self._text))

    def complete_tool_text(self) -> str:
        if self.cut_index is None:
            return ""
        return self._text[: self.cut_index].strip()

    def _forward_to(self, end: int) -> None:
        if end <= self._forwarded:
            return
        safe = self._text[self._forwarded : end]
        self._forwarded = end
        if safe:
            self.on_chunk(safe)


def _first_marker(text: str, markers: tuple[str, ...], cursor: int) -> tuple[int, str] | None:
    hits = [
        (pos, marker)
        for marker in markers
        for pos in [_first_protocol_marker_pos(text, marker, cursor)]
        if pos != -1
    ]
    return min(hits, key=lambda item: item[0]) if hits else None


def _first_tool_end_marker(text: str, body_start: int) -> tuple[int, str] | None:
    hits = [
        (pos, marker)
        for marker in _TOOL_END_MARKERS
        for pos in [_first_protocol_end_marker_pos(text, marker, body_start)]
        if pos != -1
    ]
    return min(hits, key=lambda item: item[0]) if hits else None


def _last_marker(text: str, markers: tuple[str, ...]) -> tuple[int, str] | None:
    hits = [
        (pos, marker)
        for marker in markers
        for pos in [_last_protocol_marker_pos(text, marker)]
        if pos != -1
    ]
    return max(hits, key=lambda item: item[0]) if hits else None


def _first_protocol_marker_pos(text: str, marker: str, cursor: int) -> int:
    while True:
        pos = text.find(marker, cursor)
        if pos == -1:
            return -1
        if _marker_starts_protocol_line(text, pos):
            return pos
        cursor = pos + len(marker)


def _first_protocol_end_marker_pos(text: str, marker: str, body_start: int) -> int:
    cursor = body_start
    while True:
        pos = text.find(marker, cursor)
        if pos == -1:
            return -1
        if _marker_starts_protocol_line(text, pos) or _inline_json_tool_end_marker_valid(text, body_start, pos):
            return pos
        cursor = pos + len(marker)


def _inline_json_tool_end_marker_valid(text: str, body_start: int, marker_pos: int) -> bool:
    raw = text[body_start:marker_pos].strip().strip("`")
    if not raw:
        return False
    try:
        parsed, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and not raw[end:].strip()


def _last_protocol_marker_pos(text: str, marker: str) -> int:
    cursor = len(text)
    while True:
        pos = text.rfind(marker, 0, cursor)
        if pos == -1:
            return -1
        if _marker_starts_protocol_line(text, pos):
            return pos
        cursor = pos


def _marker_starts_protocol_line(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return not text[line_start:pos].strip()


def _open_tool_start(text: str) -> tuple[int, str] | None:
    start_info = _last_marker(text, _TOOL_START_MARKERS)
    if start_info is None:
        return None
    start, marker = start_info
    if _first_tool_end_marker(text, start + len(marker)) is not None:
        return None
    return start_info


def malformed_tool_protocol_stream_abort(text: str) -> MalformedToolProtocolStreamAbort | None:
    start_info = _open_tool_start(text)
    if start_info is None:
        return None
    start, marker = start_info
    first_end = _first_tool_end_marker(text, start + len(marker))
    next_start = _first_marker(text, _TOOL_START_MARKERS, start + len(marker))
    if next_start is not None and (first_end is None or next_start[0] < first_end[0]):
        return MalformedToolProtocolStreamAbort(
            start_marker=marker,
            marker_count=_protocol_marker_count(text, _TOOL_START_MARKERS, 0),
            limit=_MAX_UNCLOSED_TOOL_START_MARKERS,
        )
    if first_end is not None:
        return None
    count = _open_tool_start_count(text)
    if count > _MAX_UNCLOSED_TOOL_START_MARKERS:
        return MalformedToolProtocolStreamAbort(
            start_marker=marker,
            marker_count=count,
            limit=_MAX_UNCLOSED_TOOL_START_MARKERS,
        )
    return None


def _open_tool_start_count(text: str) -> int:
    last_end = _last_marker(text, _TOOL_END_MARKERS)
    cursor = 0 if last_end is None else last_end[0] + len(last_end[1])
    return _protocol_marker_count(text, _TOOL_START_MARKERS, cursor)


def _protocol_marker_count(text: str, markers: tuple[str, ...], cursor: int) -> int:
    return sum(_one_protocol_marker_count(text, marker, cursor) for marker in markers)


def _one_protocol_marker_count(text: str, marker: str, cursor: int) -> int:
    count = 0
    while True:
        pos = _first_protocol_marker_pos(text, marker, cursor)
        if pos == -1:
            return count
        count += 1
        cursor = pos + len(marker)


def malformed_tool_protocol_abort_response(
    exc: MalformedToolProtocolStreamAbort, *, backend: str
) -> ModelResponse:
    payload = {
        "tool": "__parse_error__",
        "error_code": "TOOL_CALL_UNCLOSED",
        "error": (
            f"模型连续输出 {exc.marker_count} 个未闭合 {exc.start_marker} 工具协议标记；"
            "工具调用缺少结束标记"
        ),
        "raw": json.dumps(
            {
                "start_marker": exc.start_marker,
                "marker_count": exc.marker_count,
                "limit": exc.limit,
            },
            ensure_ascii=False,
        ),
    }
    return ModelResponse(
        text="[TOOL_CALL]\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "[/TOOL_CALL]",
        backend=backend,
    )


def long_write_response_abort(text: str, *, max_inline_content_chars: int) -> LongToolContentStreamAbort | None:
    return long_write_stream_abort(
        text,
        max_chars=max_inline_content_chars,
        start_info=_open_tool_start(text),
        first_end_marker=lambda cursor: _first_tool_end_marker(text, cursor),
    )


def long_write_abort_response(exc: LongToolContentStreamAbort, *, backend: str) -> ModelResponse:
    recovered = recovered_write_abort_payload(exc)
    if recovered is not None:
        return ModelResponse(
            text="[TOOL_CALL]\n"
            f"{json.dumps(recovered, ensure_ascii=False)}\n"
            "[/TOOL_CALL]",
            backend=backend,
        )
    raw = json.dumps(
        {"tool": exc.tool, "path": exc.path, "content": "...streaming content omitted..."},
        ensure_ascii=False,
    )
    payload = {
        "tool": "__parse_error__",
        "error_code": "TOOL_INLINE_CONTENT_STREAM_ABORTED",
        "error": (
            f"{exc.tool}.content inline content streaming exceeded {exc.limit} chars; "
            "工具调用缺少结束标记"
        ),
        "raw": raw,
    }
    return ModelResponse(
        text="[TOOL_CALL]\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "[/TOOL_CALL]",
        backend=backend,
    )
