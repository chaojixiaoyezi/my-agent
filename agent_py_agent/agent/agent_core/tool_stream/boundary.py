
from __future__ import annotations

"""流式 UI 转发过滤器（与统一 parser 的分层职责，G5 2026-08-10 重写）。

J.3 的「stream 与 final 共用一个解析器」在此彻底落地：本 filter 不再有
自己的 marker 状态机，结构判定（第一个块 open / 第一个完整块 / 未闭合
计数 / 长写块体起点与 close 视图）全部委托统一解析器 scan_text_blocks
（经 IncrementalTextToolParser 累积 + 每 feed 重扫），与裁决层
（tool_protocol_adapter 全文重扫）严格同一实现——任意 chunk 切分与
one-shot 的解析结果完全一致（J.7：裁决永远对完整文本重扫，视图只供
UI 转发与流式中止使用，不参与执行决策）。

本 filter 只承担两件事：
- 流式 UI 转发：只转发第一个工具块 open 之前的 prose，尾部不完整 marker
  前缀 hold-back（等后续 chunk 确认，保证可见文本与切分无关，J-4）；
- 流式中止：未闭合 open 数 > MAX_UNCLOSED_TOOL_START_MARKERS（与裁决层
  MAX_UNCLOSED_OPEN_MARKERS 同一条红线）→ MalformedToolProtocolStreamAbort；
  长写块体超限 → LongToolContentStreamAbort。执行权裁决在裁决层（G5：
  任何协议错误 → 整轮零执行），filter 永不执行、永不误放行。
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ...backends import ModelResponse
from ...backends.text_protocol_parser import (
    _TEXT_CLOSE,
    _TEXT_OPEN,
    IncrementalTextToolParser,
    scan_text_blocks,
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
    """第一个完整工具块（有有效 close）的结束位置（含 close marker）；无 → None。

    与裁决层同一 scan 实现：好块与坏块（fence 包裹等）都有 close_index，
    都在此截断——UI 不展示协议块原文。未闭合块（close_index None）不截断
    （流式等待后续 chunk）。
    """
    for block in scan_text_blocks(text).blocks:
        if block.close_index is not None:
            return block.close_index + len(_TEXT_CLOSE)
    return None


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
    _parser: IncrementalTextToolParser = field(
        default_factory=IncrementalTextToolParser, init=False, repr=False
    )

    def __call__(self, chunk: str) -> None:
        value = str(chunk or "")
        self._text += value
        self._parser.feed(value)
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
        abort = long_write_stream_abort(
            self._text,
            max_chars=self.max_inline_content_chars,
            start_info=_open_tool_start(self._text),
            first_end_marker=self._next_close,
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

    def _next_close(self, cursor: int) -> tuple[int, str] | None:
        """scan 视图：第一个 close_index >= cursor 的 (位置, marker)；无 → None。"""
        pos = self._parser.next_close_at_or_after(cursor)
        return (pos, _TEXT_CLOSE) if pos is not None else None

    def _forward_visible(self) -> None:
        # J-4：UI 只转发第一个工具块 open 之前的 prose；尾部不完整 marker 前缀
        # hold-back（等后续 chunk 确认，保证可见文本与切分无关）。
        self._forward_to(self._visible_end())

    def _visible_end(self) -> int:
        if self._first_open_index is None:
            self._first_open_index = self._parser.first_open_index()
        if self._first_open_index is not None:
            return self._first_open_index
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


def malformed_tool_protocol_stream_abort(text: str) -> MalformedToolProtocolStreamAbort | None:
    """统一 parser 视图的流式中止判定（J-6 红线：未闭合 > 1 → abort）。

    open/close/未闭合/坏块判定全部来自 scan_text_blocks（与裁决层同一实现），
    本函数只加一层 UI 视图过滤：只数 line-start 的 open（open 前仅空白）。
    这是有意设计（原 line-start 规则保留）：模型正文里同行展示的 [TOOL_CALL]
    文本不触发流式 abort；方向恒安全——裁决层用 plain find 更严，终局兜底。
    line-start 过滤是视图选择不是第二套扫描，不产生任何新的 open/close
    判定（J.7 等价性不变量：任意 chunk 切分的 abort 判定与 one-shot 一致，
    因为结构判定只依赖已稳定的 scan 结果，中间态新到达的 open/close 只让
    视图更接近终态，不会先误报再收回）。

    两种中止情形：
    - 嵌套：某个 line-start open 未闭合且其后仍有 line-start open（块序
      混乱，协议损坏，裁决层同样整轮拒）。
    - 未闭合 line-start open 数 > MAX_UNCLOSED_TOOL_START_MARKERS（与裁决层
      MAX_UNCLOSED_OPEN_MARKERS 同一条红线；line-start 子集 ≤ plain 全量，
      「流式 abort ⟹ 裁决整轮拒」恒成立）。

    尾部不完整 marker 前缀（chunk 切在 marker 中间）→ 先等后续 chunk 确认，
    不提前 abort：`[/TOOL_CAL` 可能是已存在块的闭合信号，此刻数未闭合 open
    会把可闭合的块误数成未闭合（逐字符切分 false abort 根因）。
    """
    if _trailing_marker_prefix_len(text, _TOOL_START_MARKERS + _TOOL_END_MARKERS):
        return None
    opens = _line_start_opens(scan_text_blocks(text), text)
    unclosed = [block for block in opens if block.close_index is None]
    # 嵌套：某个未闭合 line-start open 其后仍有 line-start open → 块序混乱。
    # 该判定不依赖文本推进方向：open 一旦到达即稳定（后续 close 不会移除
    # 已存在的 open 结构），逐字符与 one-shot 的 opens 集合终态相同。
    if any(index + 1 < len(opens) for index, block in enumerate(opens) if block.close_index is None):
        return MalformedToolProtocolStreamAbort(
            start_marker=_TEXT_OPEN,
            marker_count=len(unclosed),
            limit=_MAX_UNCLOSED_TOOL_START_MARKERS,
        )
    if len(unclosed) > _MAX_UNCLOSED_TOOL_START_MARKERS:
        return MalformedToolProtocolStreamAbort(
            start_marker=_TEXT_OPEN,
            marker_count=len(unclosed),
            limit=_MAX_UNCLOSED_TOOL_START_MARKERS,
        )
    return None


def _line_start_opens(scan, text: str):
    """scan 块的 line-start 视图（UI 层过滤：open 前仅空白的块才算流式协议块）。

    过滤不改变任何结构判定——open/close 位置、未闭合、坏块原因全部来自
    scan_text_blocks，这里只是挑出 line-start 的子集供 abort/长写判定用。
    """
    return [block for block in scan.blocks if _marker_starts_protocol_line(text, block.open_index)]


def _marker_starts_protocol_line(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return not text[line_start:pos].strip()


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


def _open_tool_start(text: str) -> tuple[int, str] | None:
    """最后一个「line-start 且未闭合」的 open（长写上限判定起点）；无 → None。

    委托统一 parser：close_index 由 scan 的 raw_decode 验证决定（JSON 字符串
    里的 marker 不误判闭合），这里只过滤 line-start 视图（正文同行展示的
    open 不是流式协议块）。
    """
    last = None
    for block in scan_text_blocks(text).blocks:
        if _marker_starts_protocol_line(text, block.open_index) and block.close_index is None:
            last = block.open_index
    return (last, _TEXT_OPEN) if last is not None else None


def long_write_response_abort(text: str, *, max_inline_content_chars: int) -> LongToolContentStreamAbort | None:
    """统一 parser 视图的长写中止判定（final 侧；filter 每 feed 内联同逻辑）。"""
    return long_write_stream_abort(
        text,
        max_chars=max_inline_content_chars,
        start_info=_open_tool_start(text),
        first_end_marker=lambda cursor: _next_close_from_scan(scan_text_blocks(text), cursor),
    )


def _next_close_from_scan(
    scan, cursor: int
) -> tuple[int, str] | None:
    for block in scan.blocks:
        if block.close_index is not None and block.close_index >= cursor:
            return (block.close_index, _TEXT_CLOSE)
    return None


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
