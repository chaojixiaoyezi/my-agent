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
    inline_write_content_limit,
)

_TOOL_START_MARKERS = ("[TOOL_CALL]", "[SUBAGENT_CALL]")
_TOOL_END_MARKERS = ("[/TOOL_CALL]", "[/SUBAGENT_CALL]")
_WRITE_TOOL_NAMES = {"write_file", "append_file"}
_JSON_TOOL_RE = re.compile(r'"tool"\s*:\s*"(?P<tool>write_file|append_file)"')
_JSON_PATH_RE = re.compile(r'"path"\s*:\s*"(?P<path>(?:\\.|[^"\\]){0,240})"')
_JSON_CONTENT_RE = re.compile(r'"content"\s*:\s*"')


# LLM: LongToolContentStreamAbort is a structured early-stop signal for oversized write tool streams.
# 类用途: 表示模型正在输出过长的 write_file/append_file content；上层会把它转成可恢复的分块提示。
class LongToolContentStreamAbort(RuntimeError):
    # LLM: __init__ stores structured abort metadata for the parser recovery path.
    # 函数用途: 记录被中断的工具名、路径、已流式输出字符数和上限，方便后续提示模型分块恢复。
    def __init__(self, *, tool: str, path: str, chars: int, limit: int) -> None:
        super().__init__(
            f"{tool}.content inline content streaming exceeded {limit} chars for {path or '<unknown>'}"
        )
        self.tool = tool
        self.path = path
        self.chars = chars
        self.limit = limit


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
        abort = long_write_stream_abort(self._text, max_chars=self.max_inline_content_chars)
        if abort is not None:
            raise abort
        if self.on_chunk is None:
            return
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


# LLM: long_write_stream_abort detects oversized structured write content while a tool call is still open.
# 函数用途: 根据 TOOL_CALL JSON 字段识别未闭合的大 write_file/append_file，避免等到 provider 超时才失败。
def long_write_stream_abort(text: str, *, max_chars: int | None = None) -> LongToolContentStreamAbort | None:
    limit = inline_write_content_limit(max_chars)
    start_info = _first_marker(text, _TOOL_START_MARKERS, 0)
    if start_info is None:
        return None
    start, marker = start_info
    if _first_marker(text, _TOOL_END_MARKERS, start + len(marker)) is not None:
        return None
    raw = text[start + len(marker) :]
    tool = _json_tool(raw)
    if tool not in _WRITE_TOOL_NAMES:
        return None
    content_start = _content_value_start(raw)
    if content_start is None:
        return None
    content_chars = _streamed_json_string_chars(raw[content_start:])
    if content_chars <= limit:
        return None
    return LongToolContentStreamAbort(
        tool=tool,
        path=_json_path(raw),
        chars=content_chars,
        limit=limit,
    )


# LLM: long_write_abort_response reuses the existing parse-error recovery path with bounded raw data.
# 函数用途: 把流式中断转换成标准 __parse_error__ 工具调用，让后续工具循环进入分块恢复。
def long_write_abort_response(exc: LongToolContentStreamAbort, *, backend: str) -> ModelResponse:
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


# LLM: _json_tool extracts the structured tool name from incomplete JSON without parsing the body.
# 函数用途: 从流式片段里取 write_file/append_file 工具名；只看机器字段，不读自然语言描述。
def _json_tool(raw: str) -> str:
    match = _JSON_TOOL_RE.search(raw)
    return match.group("tool") if match is not None else ""


# LLM: _content_value_start locates the start of the JSON string value without parsing incomplete JSON.
# 函数用途: 在未闭合工具调用里找到 content 字符串开头；支持顶层和 filesystem 包裹参数。
def _content_value_start(raw: str) -> int | None:
    match = _JSON_CONTENT_RE.search(raw)
    return match.end() if match is not None else None


# LLM: _streamed_json_string_chars counts a partial JSON string value without keeping the full payload.
# 函数用途: 统计流式 content 已输出的字符数；遇到未转义引号说明字符串闭合并停止。
def _streamed_json_string_chars(text: str) -> int:
    count = 0
    escaped = False
    for char in text:
        if escaped:
            count += 1
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
        count += 1
    return count


# LLM: _json_path decodes a bounded path field from an incomplete structured tool payload.
# 函数用途: 从未闭合 JSON 里取 path 字段，失败时返回短原文，避免把大正文带进恢复上下文。
def _json_path(raw: str) -> str:
    match = _JSON_PATH_RE.search(raw)
    if match is None:
        return ""
    try:
        return json.loads(f'"{match.group("path")}"')
    except json.JSONDecodeError:
        return match.group("path")
