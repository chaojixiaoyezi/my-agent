
from __future__ import annotations

"""流式 UI 转发过滤器（与统一 parser 的分层职责，F10 复核 2026-08-09）。

J.3 的「stream 与 final 共用一个解析器」在此按职责分层落地：
- 裁决层（tool_protocol_adapter.TextToolProtocolAdapter）永远对完整响应重扫
  scan_text_blocks 做最终裁决（calls/violations），filter 不参与执行决策；
- 本 filter 只承担流式 UI 转发与流式中止（abort/cut/visible text），close
  判定复用统一 parser 的 _inline_json_tool_end_marker_valid（同一实现）。

open 判定差异是有意设计：filter 用 line-start 规则（块 open 前仅空白）——
模型正文里展示示例的 [TOOL_CALL] 文本（prose 同行）不触发流式 abort 与
cut（防示例误杀）；裁决层用 plain find（更严），终局兜底。方向恒安全：
filter 不 abort 的最坏情况是裁决层晚拒（不执行），filter 永不误杀可执行
响应；abort 的未闭合计数（line-start 子集）≤ 裁决的 unclosed_count，故
「流式 abort ⟹ 裁决整轮拒」恒成立。chunk 切分等价性（J.7）由尾部不完整
marker 前缀 hold-back + 裁决全文重扫保证。
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ...backends import ModelResponse
from ...backends.text_protocol_parser import (
    _TEXT_OPEN,
    _inline_json_tool_end_marker_valid,
)
from ...tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
)
from .write_abort import (
    LongToolContentStreamAbort,
    long_write_stream_abort,
)

_TOOL_START_MARKERS = ("[TOOL_CALL]",)
_TOOL_END_MARKERS = ("[/TOOL_CALL]",)
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


@dataclass
class ToolBoundaryChunkFilter:
    on_chunk: Callable[[str], None] | None
    max_inline_content_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS
    bypass: bool = False  # native 直通：不解析 text 协议标记，全量转发
    _text: str = ""
    _forwarded: int = 0
    _closed: bool = False
    cut_detected: bool = field(default=False, init=False)
    cut_index: int | None = field(default=None, init=False)
    _first_open_index: int | None = field(default=None, init=False)

    def __call__(self, chunk: str) -> None:
        self._text += str(chunk or "")
        if self.bypass:
            if self.on_chunk is not None:
                self._forward_to(len(self._text))
            return
        # 先转发本帧已确认安全的可见部分再检查中止：abort 抛出时 UI 已推进到
        # 第一个块 open 之前，可见文本与 chunk 切分方式无关（J-4）。
        if self.on_chunk is not None:
            self._forward_visible()
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
        if cut_index is not None:
            if not self.cut_detected:
                self.cut_detected = True
                self.cut_index = cut_index
            # J-4：块体原文不进 UI；第一个完整块完成后停止转发后续内容。
            self._closed = True
            return

    def finish(self) -> None:
        if self.bypass or self.on_chunk is None or self._closed:
            return
        if self.cut_detected:
            return
        self._forward_visible()

    def complete_tool_text(self) -> str:
        if self.cut_index is None:
            return ""
        start = self._first_open_index
        if start is None:
            start = self._text.find(_TEXT_OPEN)
            if start < 0:
                return ""
        return self._text[start : self.cut_index].strip()

    def _forward_visible(self) -> None:
        # J-4：UI 只转发第一个工具块 open 之前的 prose；尾部不完整 marker 前缀
        # hold-back（等后续 chunk 确认，保证可见文本与切分无关）。
        self._forward_to(self._visible_end())

    def _visible_end(self) -> int:
        if self._first_open_index is not None:
            return self._first_open_index
        pos = self._text.find(_TEXT_OPEN)
        if pos >= 0:
            self._first_open_index = pos
            return pos
        return len(self._text) - _trailing_open_marker_prefix_len(self._text)

    def _forward_to(self, end: int) -> None:
        if end <= self._forwarded:
            return
        safe = self._text[self._forwarded : end]
        self._forwarded = end
        if safe:
            self.on_chunk(safe)


def _trailing_open_marker_prefix_len(text: str) -> int:
    """尾部是 [TOOL_CALL] 的不完整前缀（chunk 切在 marker 中间）→ 先不转发。"""
    return _trailing_marker_prefix_len(text, _TOOL_START_MARKERS)


def _trailing_marker_prefix_len(text: str, markers: tuple[str, ...]) -> int:
    """尾部是任一 marker 的不完整前缀（chunk 切在 marker 中间）→ 返回前缀长度。

    abort 判定用它作"等后续 chunk 确认"守卫：不完整的 [/TOOL_ 前缀可能是
    已有块的闭合信号，不能在中途提前 abort——保证 abort 判定与 chunk 切分
    方式无关（#89 等价性不变量）。
    """
    best = 0
    for marker in markers:
        for length in range(len(marker) - 1, 0, -1):
            if length > best and text.endswith(marker[:length]):
                best = length
    return best


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
    # 尾部是未完成 marker 前缀（chunk 切在 marker 中间）→ 先等后续 chunk 确认，
    # 不提前 abort：`[/TOOL_CAL` 可能是已存在块的闭合信号，此刻数未闭合 open
    # 会把可闭合的块误数成未闭合（逐字符切分 false abort 根因）。
    if _trailing_marker_prefix_len(text, _TOOL_START_MARKERS + _TOOL_END_MARKERS):
        return None
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
    last_end = _last_tool_end_marker(text)
    cursor = 0 if last_end is None else last_end[0] + len(last_end[1])
    return _protocol_marker_count(text, _TOOL_START_MARKERS, cursor)


def _last_tool_end_marker(text: str) -> tuple[int, str] | None:
    """最后一个已"有效闭合"的 [/TOOL_CALL]（line-start 或 raw_decode 验证的
    inline close）。未闭合 open 计数必须从它之后数——旧实现只认 line-start
    close，同行 close 的块被误数成未闭合（逐字符切分 false abort 根因）。
    """
    hits = [
        (pos, marker)
        for marker in _TOOL_END_MARKERS
        for pos in [_last_protocol_end_marker_pos(text, marker)]
        if pos != -1
    ]
    return max(hits, key=lambda item: item[0]) if hits else None


def _last_protocol_end_marker_pos(text: str, marker: str) -> int:
    cursor = len(text)
    while True:
        pos = text.rfind(marker, 0, cursor)
        if pos == -1:
            return -1
        if _marker_starts_protocol_line(text, pos):
            return pos
        open_at = text.rfind(_TEXT_OPEN, 0, pos)
        if open_at != -1 and _inline_json_tool_end_marker_valid(text, open_at + len(_TEXT_OPEN), pos):
            return pos
        cursor = pos


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
    return _protocol_violation_response(
        backend=backend,
        code="TOOL_CALL_UNCLOSED",
        detail=(
            f"模型连续输出 {exc.marker_count} 个未闭合 {exc.start_marker} 工具协议标记；"
            "没有形成可执行调用，请重新发出一个完整且独立的工具块"
        ),
        evidence={
            "start_marker": exc.start_marker,
            "marker_count": exc.marker_count,
            "limit": exc.limit,
        },
    )


def _protocol_violation_response(
    *,
    backend: str,
    code: str,
    detail: str,
    evidence: dict[str, object],
) -> ModelResponse:
    return ModelResponse(
        text="",
        backend=backend,
        tool_protocol_violations=[
            {
                "code": code,
                "detail": detail,
                "evidence_preview": json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        ],
    )


def long_write_response_abort(text: str, *, max_inline_content_chars: int) -> LongToolContentStreamAbort | None:
    return long_write_stream_abort(
        text,
        max_chars=max_inline_content_chars,
        start_info=_open_tool_start(text),
        first_end_marker=lambda cursor: _first_tool_end_marker(text, cursor),
    )


def long_write_abort_response(exc: LongToolContentStreamAbort, *, backend: str) -> ModelResponse:
    return _protocol_violation_response(
        backend=backend,
        code="TOOL_INLINE_CONTENT_STREAM_ABORTED",
        detail=(
            f"{exc.tool}.content inline content streaming exceeded {exc.limit} chars; "
            "没有执行任何写入。请把内容分成更小的完整 write_file 调用，"
            "第一块用 overwrite，后续块用 append"
        ),
        evidence={
            "source_tool": exc.tool,
            "path_sha256": hashlib.sha256(exc.path.encode("utf-8")).hexdigest(),
            "streaming_content_chars": exc.chars,
            "streaming_content_limit": exc.limit,
            "previous_write_committed": False,
        },
    )
