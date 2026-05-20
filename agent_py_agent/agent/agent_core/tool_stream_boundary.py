# LLM: Tool stream boundary handling keeps text-protocol tool calls from leaking fake transcripts.
# 模块用途: 在模型流式/文本回复里发现第一个完整工具调用后，截断后续正文，避免伪造工具结果进入控制流。

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..backends import ModelResponse
from ..tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
)
from .tool_stream_boundary_models import (
    CompleteToolCallStreamAbort,
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
)
from .tool_stream_boundary_write_abort import (
    long_write_stream_abort,
    recovered_write_abort_payload,
)

_TOOL_START_MARKERS = ("[TOOL_CALL]", "[SUBAGENT_CALL]")
_TOOL_END_MARKERS = ("[/TOOL_CALL]", "[/SUBAGENT_CALL]")
_MACHINE_BLOCK_PATTERNS = (
    re.compile(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", re.DOTALL),
    re.compile(r"\[SUBAGENT_CALL\].*?\[/SUBAGENT_CALL\]", re.DOTALL),
    re.compile(r"\[WRITE_FILE_RAW[^\]]*\].*?\[/WRITE_FILE_RAW\]", re.DOTALL),
    re.compile(
        r"\[FILE_WRITE_SESSION_APPEND[^\]]*\].*?\[/FILE_WRITE_SESSION_APPEND\]",
        re.DOTALL,
    ),
)
_MAX_UNCLOSED_TOOL_START_MARKERS = 1
_MAX_NEAR_TOOL_PROTOCOL_LINES = 7
_NEAR_TOOL_PROTOCOL_LINE_RE = re.compile(r"(?m)^\s*(?:\[|<)?\s*TOOL(?:\b|_|\])")


# LLM: first_complete_tool_call_cut_index finds the text boundary after the first closed tool block.
# 函数用途: 返回第一个完整工具调用块结束后的位置；没有闭合工具块时返回 None，保留原有未闭合 JSON 恢复逻辑。
def first_complete_tool_call_cut_index(text: str) -> int | None:
    start_info = _first_marker(text, _TOOL_START_MARKERS, 0)
    if start_info is None:
        return None
    start, marker = start_info
    end_info = _first_marker(text, _TOOL_END_MARKERS, start + len(marker))
    if end_info is None:
        return None
    end, end_marker = end_info
    return end + len(end_marker)


# LLM: cut_response_after_first_complete_tool_call preserves machine blocks and removes surrounding prose.
# 函数用途: 模型回复包含工具块时，只保留结构化机器块，防止普通自然语言成为控制流事实。
def cut_response_after_first_complete_tool_call(response: ModelResponse) -> tuple[ModelResponse, bool]:
    ranges = _complete_machine_block_ranges(response.text)
    if not ranges:
        return response, False
    machine_text = "\n".join(response.text[start:end].strip() for start, end in ranges)
    if machine_text == response.text.strip():
        return response, False
    return ModelResponse(text=machine_text, backend=response.backend), True


# LLM: _complete_machine_block_ranges finds explicit executable protocol blocks only.
# 函数用途: 提取 TOOL_CALL、SUBAGENT_CALL 和 raw-write blocks 的范围；不读取普通文本语义。
def _complete_machine_block_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for pattern in _MACHINE_BLOCK_PATTERNS:
        ranges.extend((match.start(), match.end()) for match in pattern.finditer(text))
    ranges.sort(key=lambda item: item[0])
    return _non_overlapping_ranges(ranges)


# LLM: _non_overlapping_ranges avoids double-counting nested or overlapping protocol matches.
# 函数用途: 按文本顺序保留不重叠机器块，保持工具执行顺序稳定。
def _non_overlapping_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    kept: list[tuple[int, int]] = []
    last_end = -1
    for start, end in ranges:
        if start < last_end:
            continue
        kept.append((start, end))
        last_end = end
    return kept


# LLM: ToolBoundaryChunkFilter hides streamed text after the first complete tool-call boundary.
# 类用途: 包装 on_chunk 回调；一旦第一个工具调用闭合，就停止把后面的模型自述/伪造回执展示给用户。
@dataclass
class ToolBoundaryChunkFilter:
    on_chunk: Callable[[str], None] | None
    max_inline_content_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS
    _text: str = ""
    _forwarded: int = 0
    _closed: bool = False
    cut_detected: bool = field(default=False, init=False)

    # LLM: __call__ forwards only text that belongs before or inside the first complete tool block.
    # 函数用途: 接收模型流式片段，实时累计并在工具调用边界后静默丢弃后续可见输出。
    def __call__(self, chunk: str) -> None:
        if self._closed:
            return
        self._text += str(chunk or "")
        protocol_abort = malformed_tool_protocol_stream_abort(self._text)
        if protocol_abort is not None:
            raise protocol_abort
        start_info = _first_marker(self._text, _TOOL_START_MARKERS, 0)
        abort = long_write_stream_abort(
            self._text,
            max_chars=self.max_inline_content_chars,
            start_info=start_info,
            first_end_marker=lambda cursor: _first_marker(self._text, _TOOL_END_MARKERS, cursor),
        )
        if abort is not None:
            raise abort
        cut_index = first_complete_tool_call_cut_index(self._text)
        if cut_index is None:
            if self.on_chunk is not None:
                self._forward_to(len(self._text))
            return
        self.cut_detected = True
        if self.on_chunk is not None:
            self._forward_to(cut_index)
        self._closed = True
        raise CompleteToolCallStreamAbort(text=self._text[:cut_index], cut_index=cut_index)

    # LLM: finish flushes any ordinary non-tool response that never crossed a tool boundary.
    # 函数用途: 模型没有工具调用时，确保最后残留文本仍能正常显示；已截断时不再输出后续文本。
    def finish(self) -> None:
        if self.on_chunk is None or self._closed:
            return
        self._forward_to(len(self._text))

    # LLM: _forward_to preserves chunk order while avoiding duplicate visible output.
    # 函数用途: 将累计文本中尚未转发的安全片段交给原始 on_chunk。
    def _forward_to(self, end: int) -> None:
        if end <= self._forwarded:
            return
        safe = self._text[self._forwarded : end]
        self._forwarded = end
        if safe:
            self.on_chunk(safe)


# LLM: _first_marker returns the earliest matching marker and its text.
# 函数用途: 在多个工具边界标记里找最早出现的位置，保持 TOOL_CALL 和 SUBAGENT_CALL 兼容。
def _first_marker(text: str, markers: tuple[str, ...], cursor: int) -> tuple[int, str] | None:
    hits = [
        (pos, marker)
        for marker in markers
        for pos in [text.find(marker, cursor)]
        if pos != -1
    ]
    return min(hits, key=lambda item: item[0]) if hits else None


# LLM: malformed_tool_protocol_stream_abort detects repeated unclosed tool start markers from the machine protocol.
# 函数用途: 如果模型反复输出 TOOL_CALL/SUBAGENT_CALL 开始标记但没有任何结束标记，提前中断防止拖到总超时。
def malformed_tool_protocol_stream_abort(text: str) -> MalformedToolProtocolStreamAbort | None:
    start_info = _first_marker(text, _TOOL_START_MARKERS, 0)
    near_count = _near_tool_protocol_line_count(text)
    if start_info is None:
        if near_count <= _MAX_NEAR_TOOL_PROTOCOL_LINES:
            return None
        return MalformedToolProtocolStreamAbort(
            start_marker="TOOL_PROTOCOL_LINE",
            marker_count=near_count,
            limit=_MAX_NEAR_TOOL_PROTOCOL_LINES,
        )
    start, marker = start_info
    first_end = _first_marker(text, _TOOL_END_MARKERS, start + len(marker))
    next_start = _first_marker(text, _TOOL_START_MARKERS, start + len(marker))
    if next_start is not None and (first_end is None or next_start[0] < first_end[0]):
        return MalformedToolProtocolStreamAbort(
            start_marker=marker,
            marker_count=sum(text.count(item) for item in _TOOL_START_MARKERS),
            limit=_MAX_UNCLOSED_TOOL_START_MARKERS,
        )
    if first_end is not None:
        return None
    count = sum(text.count(item) for item in _TOOL_START_MARKERS)
    if count > _MAX_UNCLOSED_TOOL_START_MARKERS:
        return MalformedToolProtocolStreamAbort(
            start_marker=marker,
            marker_count=count,
            limit=_MAX_UNCLOSED_TOOL_START_MARKERS,
        )
    if near_count <= _MAX_NEAR_TOOL_PROTOCOL_LINES:
        return None
    return MalformedToolProtocolStreamAbort(
        start_marker="TOOL_PROTOCOL_LINE",
        marker_count=near_count,
        limit=_MAX_NEAR_TOOL_PROTOCOL_LINES,
    )


# LLM: _near_tool_protocol_line_count catches protocol-shaped marker storms that are not valid openers.
# 函数用途: 统计行首 TOOL/[TOOL/<TOOL 这类机器协议碎片；不匹配普通大小写自然语言 Tool 文本。
def _near_tool_protocol_line_count(text: str) -> int:
    return len(_NEAR_TOOL_PROTOCOL_LINE_RE.findall(text))


# LLM: malformed_tool_protocol_abort_response reports protocol marker storms through the normal parser recovery tool.
# 函数用途: 把连续未闭合工具标记转为标准 __parse_error__，避免把半截协议文本当成事实或继续展示给用户。
def malformed_tool_protocol_abort_response(
    exc: MalformedToolProtocolStreamAbort, *, backend: str
) -> ModelResponse:
    payload = {
        "tool": "__parse_error__",
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


# LLM: complete_tool_call_abort_response returns the first executable tool block after stream early-stop.
# 函数用途: 把完整工具块早停信号转成普通 ModelResponse，让后续工具循环按既有解析路径执行工具。
def complete_tool_call_abort_response(
    exc: CompleteToolCallStreamAbort, *, backend: str
) -> ModelResponse:
    return ModelResponse(text=exc.text, backend=backend)


# LLM: long_write_abort_response reuses the existing parse-error recovery path with bounded raw data.
# 函数用途: 把流式中断转换成标准 __parse_error__ 工具调用，让后续工具循环进入分块恢复。
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
