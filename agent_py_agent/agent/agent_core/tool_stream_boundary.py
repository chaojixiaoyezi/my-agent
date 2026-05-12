# LLM: Tool stream boundary handling keeps text-protocol tool calls from leaking fake transcripts.
# 模块用途: 在模型流式/文本回复里发现第一个完整工具调用后，截断后续正文，避免伪造工具结果进入控制流。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..backends import ModelResponse

_TOOL_START_MARKERS = ("[TOOL_CALL]", "[SUBAGENT_CALL]")
_TOOL_END_MARKERS = ("[/TOOL_CALL]", "[/SUBAGENT_CALL]")


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


# LLM: cut_response_after_first_complete_tool_call removes model prose after an executable tool block.
# 函数用途: 把模型回复裁到第一个完整工具调用结束处，防止后续伪造 tool-output-record 或多余调用被采纳。
def cut_response_after_first_complete_tool_call(response: ModelResponse) -> tuple[ModelResponse, bool]:
    cut_index = first_complete_tool_call_cut_index(response.text)
    if cut_index is None or cut_index >= len(response.text):
        return response, False
    return ModelResponse(text=response.text[:cut_index], backend=response.backend), True


# LLM: ToolBoundaryChunkFilter hides streamed text after the first complete tool-call boundary.
# 类用途: 包装 on_chunk 回调；一旦第一个工具调用闭合，就停止把后面的模型自述/伪造回执展示给用户。
@dataclass
class ToolBoundaryChunkFilter:
    on_chunk: Callable[[str], None] | None
    _text: str = ""
    _forwarded: int = 0
    _closed: bool = False
    cut_detected: bool = field(default=False, init=False)

    # LLM: __call__ forwards only text that belongs before or inside the first complete tool block.
    # 函数用途: 接收模型流式片段，实时累计并在工具调用边界后静默丢弃后续可见输出。
    def __call__(self, chunk: str) -> None:
        if self.on_chunk is None or self._closed:
            return
        self._text += str(chunk or "")
        cut_index = first_complete_tool_call_cut_index(self._text)
        if cut_index is None:
            self._forward_to(len(self._text))
            return
        self.cut_detected = True
        self._forward_to(cut_index)
        self._closed = True

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
