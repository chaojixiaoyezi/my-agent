# LLM: Tool stream boundary handling keeps text-protocol tool calls from leaking fake transcripts.
# 模块用途: 在模型流式/文本回复里发现第一个完整工具调用后，截断后续正文，避免伪造工具结果进入控制流。

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256

from ..backends import ModelResponse
from ..tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
    streaming_inline_write_abort_limit,
)
from ..tooling.file_write_session_models import DEFAULT_MAX_SESSION_CHUNK_CHARS

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
_WRITE_TOOL_NAMES = {"write_file", "append_file", "file_write_session"}
_MAX_UNCLOSED_TOOL_START_MARKERS = 1
_MAX_NEAR_TOOL_PROTOCOL_LINES = 7
_JSON_TOOL_RE = re.compile(r'"tool"\s*:\s*"(?P<tool>write_file|append_file|file_write_session)"')
_JSON_ACTION_RE = re.compile(r'"action"\s*:\s*"(?P<action>(?:\\.|[^"\\]){0,32})"')
_JSON_PATH_RE = re.compile(r'"(?:path|target_path)"\s*:\s*"(?P<path>(?:\\.|[^"\\]){0,240})"')
_JSON_SESSION_RE = re.compile(r'"session_id"\s*:\s*"(?P<session_id>(?:\\.|[^"\\]){0,80})"')
_JSON_CHUNK_INDEX_RE = re.compile(r'"chunk_index"\s*:\s*(?P<chunk_index>\d{1,9})')
_JSON_CONTENT_RE = re.compile(r'"content"\s*:\s*"')
_NEAR_TOOL_PROTOCOL_LINE_RE = re.compile(r"(?m)^\s*(?:\[|<)?\s*TOOL(?:\b|_|\])")


# LLM: LongToolContentStreamAbort is a structured early-stop signal for oversized write tool streams.
# 类用途: 表示模型正在输出过长的 write_file/append_file content；上层会把它转成可恢复的分块提示。
class LongToolContentStreamAbort(RuntimeError):
    # LLM: __init__ stores structured abort metadata for the parser recovery path.
    # 函数用途: 记录被中断的工具名、路径、已流式输出字符数和上限，方便后续提示模型分块恢复。
    def __init__(
        self,
        *,
        tool: str,
        path: str,
        chars: int,
        limit: int,
        action: str = "",
        session_id: str = "",
        chunk_index: int | None = None,
        content_prefix: str = "",
    ) -> None:
        super().__init__(
            f"{tool}.content inline content streaming exceeded {limit} chars for {path or '<unknown>'}"
        )
        self.tool = tool
        self.path = path
        self.chars = chars
        self.limit = limit
        self.action = action
        self.session_id = session_id
        self.chunk_index = chunk_index
        self.content_prefix = content_prefix


# LLM: MalformedToolProtocolStreamAbort stops repeated protocol markers before they burn the full request timeout.
# 类用途: 表示模型连续输出未闭合工具调用标记；上层会转成结构化 parse error 让下一轮恢复。
class MalformedToolProtocolStreamAbort(RuntimeError):
    # LLM: __init__ stores marker storm facts without trusting the surrounding model text.
    # 函数用途: 记录异常工具协议标记、出现次数和阈值，方便日志和恢复提示说明问题来源。
    def __init__(self, *, start_marker: str, marker_count: int, limit: int) -> None:
        super().__init__(
            f"tool protocol emitted {marker_count} unclosed {start_marker} markers"
        )
        self.start_marker = start_marker
        self.marker_count = marker_count
        self.limit = limit


# LLM: CompleteToolCallStreamAbort makes the first closed tool block a provider-control boundary.
# 类用途: 表示模型已经流出一个完整可执行工具调用；上层应立即停止继续消费模型输出并执行该工具。
class CompleteToolCallStreamAbort(RuntimeError):
    # LLM: __init__ stores the exact first tool block without trusting any later streamed text.
    # 函数用途: 记录第一个完整工具调用文本和截断位置，供模型调用边界直接返回给工具循环。
    def __init__(self, *, text: str, cut_index: int) -> None:
        super().__init__("complete tool call streamed")
        self.text = text
        self.cut_index = cut_index


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
        abort = long_write_stream_abort(self._text, max_chars=self.max_inline_content_chars)
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


# LLM: long_write_stream_abort detects oversized structured write content while a tool call is still open.
# 函数用途: 根据 TOOL_CALL JSON 字段识别未闭合的大 write_file/append_file，避免等到 provider 超时才失败。
def long_write_stream_abort(text: str, *, max_chars: int | None = None) -> LongToolContentStreamAbort | None:
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
    limit = _streaming_write_abort_limit(tool, max_chars)
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
        action=_json_action(raw),
        session_id=_json_session_id(raw),
        chunk_index=_json_chunk_index(raw),
        content_prefix=_streamed_json_string_prefix(raw[content_start:], max_chars=limit + 2048),
    )


# LLM: file_write_session is the structured large-file channel and gets a larger stream ceiling than write_file.
# 函数用途: 根据工具类型计算未闭合 content 早停阈值；普通写入早停，大文件 session 按 chunk 上限早停。
def _streaming_write_abort_limit(tool: str, max_chars: int | None) -> int:
    limit = streaming_inline_write_abort_limit(max_chars)
    if tool != "file_write_session":
        return limit
    return max(limit, DEFAULT_MAX_SESSION_CHUNK_CHARS)


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
    recovered = _recovered_file_write_session_append(exc) or _recovered_write_file_append(exc)
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


# LLM: _recovered_file_write_session_append converts a partial structured write stream into real work.
# 函数用途: 对 file_write_session.append 的未闭合 content 前缀执行一次真实 append，避免大内容反复解析失败。
def _recovered_file_write_session_append(
    exc: LongToolContentStreamAbort,
) -> dict[str, object] | None:
    if exc.tool != "file_write_session" or exc.action != "append":
        return None
    if not exc.session_id or exc.chunk_index is None or not exc.content_prefix:
        return None
    return {
        "tool": "file_write_session",
        "action": "append",
        "session_id": exc.session_id,
        "chunk_index": exc.chunk_index,
        "content": exc.content_prefix,
    }


# LLM: _recovered_write_file_append preserves the first streamed write_file bytes as a session chunk.
# 函数用途: write_file 大正文未闭合时，把已生成前缀转成可恢复 file_write_session append。
def _recovered_write_file_append(exc: LongToolContentStreamAbort) -> dict[str, object] | None:
    if exc.tool != "write_file" or not exc.path or not exc.content_prefix:
        return None
    return {
        "tool": "file_write_session",
        "action": "append",
        "session_id": _stream_write_session_id(exc.path),
        "target_path": exc.path,
        "chunk_index": 0,
        "content": exc.content_prefix,
    }


# LLM: _stream_write_session_id derives a stable safe id from the target path.
# 函数用途: 让同一路径的流式 write_file 恢复到同一 session，避免重复新建临时目录。
def _stream_write_session_id(path: str) -> str:
    digest = sha256(path.encode("utf-8")).hexdigest()[:12]
    return f"stream_write_{digest}"


# LLM: _json_path decodes a bounded path field from an incomplete structured tool payload.
# 函数用途: 从未闭合 JSON 里取 path 字段，失败时返回短原文，避免把大正文带进恢复上下文。
def _json_path(raw: str) -> str:
    match = _JSON_PATH_RE.search(raw)
    if match is None:
        return _json_session(raw)
    try:
        return json.loads(f'"{match.group("path")}"')
    except json.JSONDecodeError:
        return match.group("path")


# LLM: _json_session gives file_write_session aborts a stable short identity when no path is present.
# 函数用途: 从未闭合 JSON 里提取 session_id，避免恢复提示只显示 `<unknown>`。
def _json_session(raw: str) -> str:
    match = _JSON_SESSION_RE.search(raw)
    if match is None:
        return ""
    try:
        session_id = json.loads(f'"{match.group("session_id")}"')
    except json.JSONDecodeError:
        session_id = match.group("session_id")
    return f"session_id={session_id}" if session_id else ""


# LLM: _json_action decodes the file_write_session action from an incomplete machine payload.
# 函数用途: 只读取 action 结构化字段；不能识别时返回空字符串。
def _json_action(raw: str) -> str:
    match = _JSON_ACTION_RE.search(raw)
    if match is None:
        return ""
    try:
        return str(json.loads(f'"{match.group("action")}"'))
    except json.JSONDecodeError:
        return match.group("action")


# LLM: _json_session_id extracts the raw session id without the display prefix.
# 函数用途: 给恢复 append 构造真实工具参数；失败时返回空字符串。
def _json_session_id(raw: str) -> str:
    match = _JSON_SESSION_RE.search(raw)
    if match is None:
        return ""
    try:
        return str(json.loads(f'"{match.group("session_id")}"'))
    except json.JSONDecodeError:
        return match.group("session_id")


# LLM: _json_chunk_index extracts chunk index from incomplete machine JSON.
# 函数用途: 给恢复 append 构造真实 chunk_index；没有结构化字段时返回 None。
def _json_chunk_index(raw: str) -> int | None:
    match = _JSON_CHUNK_INDEX_RE.search(raw)
    if match is None:
        return None
    try:
        return int(match.group("chunk_index"))
    except ValueError:
        return None


# LLM: _streamed_json_string_prefix decodes a bounded prefix of an incomplete JSON string value.
# 函数用途: 从未闭合 content 字符串中恢复已流出的机器载荷，不读取周围自然语言。
def _streamed_json_string_prefix(text: str, *, max_chars: int) -> str:
    raw_parts: list[str] = []
    escaped = False
    decoded_chars = 0
    for char in text:
        if decoded_chars >= max_chars:
            break
        if escaped:
            raw_parts.append("\\" + char)
            escaped = False
            decoded_chars += 1
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
        raw_parts.append(char)
        decoded_chars += 1
    raw_prefix = "".join(raw_parts)
    try:
        return str(json.loads(f'"{raw_prefix}"'))
    except json.JSONDecodeError:
        return raw_prefix.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
