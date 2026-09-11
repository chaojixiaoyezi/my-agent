# LLM: 解析供应商流为 typed delta/completion，保留原生字段存在性，不做正文去重或任务控制。
# 模块用途: 将 SSE 分为正文、思考、工具参数和用量；修改时检查流式结束与下轮历史回放。
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from ..conversation.tool_input_progress import TOOL_INPUT_PROGRESS_SCHEMA


# LLM: 流事件同时承载用户可见文本、规范化工具调用和仅供下一轮回放的厂商原生 assistant 块；三类消费者必须彼此隔离。
# 类用途: 表示一条解析后的模型流事件，供文本展示、工具执行和内部历史分别读取。
@dataclass(frozen=True)
class StreamEvent:
    """One normalized stream event with optional visible text and usage metadata.

    ``tool_use_block`` is populated on native Anthropic/OpenAI tool-call paths,
    once a tool_use content block has finished accumulating its input JSON; it
    holds ``{"id","name","input"}``. Text/usage consumers ignore it.
    ``thinking_content`` carries live thinking deltas for rich transcript sinks;
    text/usage consumers ignore it.
    ``tool_input_progress`` contains counters only; partial tool JSON never leaves
    this parser through the display callback.
    """

    content: str = ""
    usage: dict[str, Any] | None = None
    tool_use_block: dict[str, Any] | None = None
    assistant_content_block: dict[str, Any] | None = None
    thinking_content: str = ""
    tool_input_progress: dict[str, Any] | None = None


# LLM: 这是流结束的结构化事实；assistant_content_blocks 不直接作为可见 chunk，显式 rich transcript 只能由上层收尾筛出 type=thinking 正文。
# 类用途: 汇总一条流是否完整、停止原因，以及下一轮请求需要原样续接的有序 assistant 块。
@dataclass(frozen=True)
class StreamCompletion:
    """流结束事实；参数错误与缺失终态分开，长度/过滤继续保留供应商 stop_reason。"""

    saw_message_stop: bool = False
    stop_reason: str = ""
    open_tool_buffer: bool = False
    assistant_content_blocks: tuple[dict[str, Any], ...] = ()

    # LLM: 只检查供应商结构事实；不把坏 JSON 或断流猜成 token 上限。
    # 函数用途: 返回供应商停止原因之外的完整性错误，供公共响应合同分类。
    @property
    def incomplete_reason(self) -> str:
        if self.open_tool_buffer:
            return "invalid_tool_arguments"
        return "" if self.saw_message_stop or self.stop_reason else "stream_eof"

    # LLM: 布尔仅决定零工具执行，不决定续跑类型；真实原因由 incomplete_reason/stop_reason 保留。
    # 函数用途: 供既有消费者查询本次响应是否不完整。
    @property
    def truncated(self) -> bool:
        return bool(self.incomplete_reason or self.stop_reason in {"length", "max_tokens", "content_filter"})


def openai_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    """Yield visible text chunks from OpenAI-compatible SSE data lines."""
    for event in openai_stream_events(lines):
        if event.content:
            yield event.content


# LLM: SSE delta 是增量；工具参数过程只投影计数，完整工具仍在流结束解析，不能据进度提前执行。
# 函数用途: 分离正文、思考和工具准备进度，保持空 thinking 字段及多工具独立序号。
def openai_stream_events(lines: Iterable[str]) -> Iterator[StreamEvent]:
    tool_acc = _OpenAIToolCallAccumulator()
    reasoning_parts: list[str] = []
    reasoning_present = False
    saw_done = False
    finish_reason = ""
    for line in lines:
        if line == "[DONE]":
            saw_done = True
            break
        obj = json_object_or_none(line)
        if obj is None:
            continue
        choices = obj.get("choices", [])
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta", {}) if isinstance(choice.get("delta"), dict) else {}
        content = delta.get("content")
        reasoning = delta.get("reasoning_content")
        reasoning_present = reasoning_present or "reasoning_content" in delta
        finish_reason = str(choice.get("finish_reason") or "") or finish_reason
        usage = _usage_dict(obj.get("usage"))
        if isinstance(reasoning, str) and reasoning:
            reasoning_parts.append(reasoning)
            yield StreamEvent(thinking_content=reasoning)
        for progress in tool_acc.consume(delta.get("tool_calls")):
            yield StreamEvent(tool_input_progress=progress)
        if content or usage:
            yield StreamEvent(content=str(content or ""), usage=usage or None)
    for progress in tool_acc.completed_progress():
        yield StreamEvent(tool_input_progress=progress)
    blocks, parse_failed = tool_acc.finish()
    for block in blocks:
        yield StreamEvent(tool_use_block=block)
    return StreamCompletion(
        saw_message_stop=saw_done,
        stop_reason=finish_reason,
        open_tool_buffer=parse_failed,
        assistant_content_blocks=(
            ({"type": "thinking", "thinking": "".join(reasoning_parts)},)
            if reasoning_present
            else ()
        ),
    )


# LLM: OpenAI tool index 是流内身份，原始 JSON 只在累积器内保存；观察器不能取得半截参数。
# 类用途: 分别拼接并行工具参数，并对外提供不含业务内容的字符进度。
class _OpenAIToolCallAccumulator:
    # LLM: 每个响应独立实例，禁止跨模型调用复用工具缓冲或序号。
    # 函数用途: 初始化单次响应的工具参数缓冲。
    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    # LLM: 这里只接受供应商结构化 index/name/arguments；返回计数不代表 JSON 合法或调用已执行。
    # 函数用途: 收集本帧各工具的增量，并返回开始或进行中的脱敏快照。
    def consume(self, raw_calls: object) -> list[dict[str, Any]]:
        progress = []
        if not isinstance(raw_calls, list):
            return progress
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("index", 0) or 0)
            except (TypeError, ValueError):
                continue
            new = index not in self._calls
            call = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call["id"] += str(raw.get("id") or "")
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            call["name"] += str(function.get("name") or "")
            call["arguments"] += str(function.get("arguments") or "")
            progress.append(self._progress(index, call, "started" if new else "streaming"))
        return progress

    # LLM: ready 仅关闭临时展示；finish 仍独立检查完整 JSON，截断不能通过此事件获得执行许可。
    # 函数用途: 在流收尾时关闭每个已开始工具的准备进度。
    def completed_progress(self) -> list[dict[str, Any]]:
        return [self._progress(index, call, "ready") for index, call in sorted(self._calls.items())]

    # LLM: 白名单与 Anthropic 共用，不暴露 call id、路径、JSON 或正文，也不建立新工具账。
    # 函数用途: 生成可供界面节流器使用的工具名、序号及累计字符数。
    @staticmethod
    def _progress(index: int, call: dict[str, str], phase: str) -> dict[str, Any]:
        return {"schema": TOOL_INPUT_PROGRESS_SCHEMA, "phase": phase, "stream_index": index,
                "tool": call["name"] or "Tool", "received_chars": len(call["arguments"])}

    # LLM: 参数必须是完整 JSON 对象；坏块不能猜成空对象，整轮失败由响应边界统一处理。
    # 函数用途: 将各工具 JSON 转为标准调用块，保持原 index 顺序和不完整信号。
    def finish(self) -> tuple[list[dict[str, Any]], bool]:
        blocks: list[dict[str, Any]] = []
        parse_failed = False
        for index in sorted(self._calls):
            call = self._calls[index]
            try:
                parsed = json.loads(call["arguments"])
            except (json.JSONDecodeError, TypeError):
                parse_failed = True
                continue
            if not isinstance(parsed, dict):
                parse_failed = True
                continue
            blocks.append({"id": call["id"], "name": call["name"], "input": parsed})
        return blocks, parse_failed


def anthropic_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    """Yield visible text chunks from Anthropic-compatible SSE data lines."""
    for event in anthropic_stream_events(lines):
        if event.content:
            yield event.content


# LLM: 解析器必须按 content_block 的真实顺序保存 thinking/text/tool_use，同时只把 text_delta 放进用户可见 content。
# 函数用途: 逐条解析 Anthropic SSE，并在结束时返回完整性与可回放 assistant 历史。
def anthropic_stream_events(lines: Iterable[str]) -> Iterator[StreamEvent]:
    """Yield normalized Anthropic SSE events; return a ``StreamCompletion`` health check.

    The generator return value (captured via ``StopIteration.value`` / ``yield from``)
    reports whether the stream reached ``message_stop``, the final ``stop_reason``, and
    whether a tool_use input-JSON buffer was still open at the end. Native callers use it
    to flag truncation; text callers ignore it (unchanged behavior).
    """
    tool_acc = _AnthropicToolUseAccumulator()
    assistant_acc = _AnthropicAssistantContentAccumulator()
    saw_message_stop = False
    stop_reason = ""
    for line in lines:
        obj = json_object_or_none(line)
        if obj is None:
            continue
        event_type = obj.get("type", "")
        if event_type == "message_stop":
            saw_message_stop = True
            break
        if event_type == "message_delta":
            stop_reason = str(obj.get("delta", {}).get("stop_reason", "") or "") or stop_reason
        block, tool_input_progress = tool_acc.consume(event_type, obj)
        assistant_block = assistant_acc.consume(event_type, obj)
        if (
            block is not None
            or assistant_block is not None
            or tool_input_progress is not None
        ):
            yield StreamEvent(
                tool_use_block=block,
                assistant_content_block=assistant_block,
                tool_input_progress=tool_input_progress,
            )
            continue
        text = obj.get("delta", {}).get("text", "") if event_type == "content_block_delta" else ""
        thinking = (
            obj.get("delta", {}).get("thinking", "")
            if event_type == "content_block_delta"
            and obj.get("delta", {}).get("type") == "thinking_delta"
            else ""
        )
        usage = _anthropic_usage(obj, event_type)
        if text or usage:
            yield StreamEvent(content=str(text or ""), usage=usage or None)
        elif thinking:
            yield StreamEvent(thinking_content=str(thinking or ""))
    return StreamCompletion(
        saw_message_stop=saw_message_stop,
        stop_reason=stop_reason,
        open_tool_buffer=tool_acc.has_open_buffer(),
        assistant_content_blocks=assistant_acc.completed_blocks(),
    )


# LLM: 单响应累积器必须保留所有已见错误，后续好块不能清掉坏块事实；不外发半截输入。
# 类用途: 拼接 Anthropic 工具块，仅完整对象生成工具候选，整轮由响应边界统一判定。
class _AnthropicToolUseAccumulator:
    """Accumulates Anthropic streamed tool_use blocks across SSE events.

    A tool_use block arrives as ``content_block_start`` (carries id/name),
    one or more ``content_block_delta`` with ``input_json_delta.partial_json``
    fragments, then ``content_block_stop``. We buffer the partial JSON and
    parse it on stop, returning the finished ``{"id","name","input"}`` block.
    """

    # LLM: 每次请求独立创建，错误标记在这一整个响应内保持单调。
    # 函数用途: 初始化当前工具与本轮失败事实。
    def __init__(self) -> None:
        self._index: int | None = None
        self._id: str = ""
        self._name: str = ""
        self._buffer: str = ""
        self._last_parse_failed: bool = False
        self._initial_input: dict[str, Any] = {}

    def has_open_buffer(self) -> bool:
        """流结束时是否仍有半截的 tool_use 参数缓冲。

        两种残缺都算未闭合：①最后一个 tool_use 块开了头但没等到 content_block_stop
        （``_index`` 仍非 None）；②收到了 content_block_stop，但缓冲里是不合法/半截 JSON
        （``json.loads`` 失败）。任一成立 → 该工具调用的参数 JSON 被截断。
        """
        if self._index is not None:
            return True
        return self._last_parse_failed

    # LLM: 返回的 progress 只能含累计字符数等结构化计数；partial_json 必须始终
    # 留在该累积器内部，直到 stop 后解析成真正 tool_use block。
    # 函数用途: 吃入一条 Anthropic 工具块事件，同时给展示层提供不含参数正文的进度。
    def consume(
        self,
        event_type: str,
        obj: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if event_type == "content_block_start":
            self._on_start(obj)
            return None, self._progress_payload("started")
        if event_type == "content_block_delta":
            before = len(self._buffer)
            self._on_delta(obj)
            progress = (
                self._progress_payload("streaming")
                if len(self._buffer) > before
                else None
            )
            return None, progress
        if event_type == "content_block_stop":
            progress = (
                self._progress_payload("ready")
                if self._index is not None and obj.get("index") == self._index
                else None
            )
            return self._on_stop(obj), progress
        return None, None

    # LLM: stream_index、tool 和 buffer 长度是唯一允许外发的 provider 事实；
    # tool_use id 与缓冲正文都不进入 payload，避免意外形成第二份工具调用合同。
    # 函数用途: 为当前尚未闭合的工具块生成一条脱敏进度快照。
    def _progress_payload(self, phase: str) -> dict[str, Any] | None:
        if self._index is None:
            return None
        try:
            stream_index = max(0, int(self._index))
        except (TypeError, ValueError):
            return None
        return {
            "schema": TOOL_INPUT_PROGRESS_SCHEMA,
            "phase": phase,
            "stream_index": stream_index,
            "tool": self._name or "Tool",
            "received_chars": len(self._buffer),
        }

    # LLM: 后续块不能清除前块错误；未闭合块被新块覆盖同样记为协议损坏。
    # 函数用途: 记录新工具原始身份和初始对象，不猜补非对象输入。
    def _on_start(self, obj: dict[str, Any]) -> None:
        self._last_parse_failed = self._last_parse_failed or self._index is not None
        block = obj.get("content_block")
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            self._index = None
            return
        self._index = obj.get("index")
        self._id = str(block.get("id", "") or "")
        self._name = str(block.get("name", "") or "")
        self._buffer = ""
        initial = block.get("input", {})
        self._initial_input = initial if isinstance(initial, dict) else {}
        self._last_parse_failed = self._last_parse_failed or not isinstance(initial, dict)

    # LLM: 参数增量必须属于同一 block index，防止其它内容块污染工具输入。
    # 函数用途: 追加当前工具的原始 JSON 增量，不执行或发布参数内容。
    def _on_delta(self, obj: dict[str, Any]) -> None:
        if self._index is None or obj.get("index") != self._index:
            return
        delta = obj.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
            self._buffer += str(delta.get("partial_json", "") or "")

    # LLM: 任意坏块只返回失败标记；空增量沿用 start 的真实对象，不猜补残缺 JSON。
    # 函数用途: 关闭一个工具候选并保留整个响应的损坏事实。
    def _on_stop(self, obj: dict[str, Any]) -> dict[str, Any] | None:
        if self._index is None or obj.get("index") != self._index:
            return None
        parsed, parse_failed = _parse_tool_input(self._buffer) if self._buffer else (self._initial_input, False)
        self._last_parse_failed = self._last_parse_failed or parse_failed
        block = {"id": self._id, "name": self._name, "input": parsed}
        self._index = None
        self._buffer = ""
        return None if parse_failed else block


# LLM: 该累积器是 Anthropic 流式响应块进入下一轮请求的白名单边界；只接受协议允许回放的四类块并保留到达顺序。
# 类用途: 把分散的 thinking、签名、文本和工具参数增量还原成完整 content blocks。
class _AnthropicAssistantContentAccumulator:
    def __init__(self) -> None:
        self._index: int | None = None
        self._block: dict[str, Any] | None = None
        self._input_buffer = ""
        self._completed: list[dict[str, Any]] = []

    # LLM: 返回值要与已完成顺序一致，并复制外层 dict，避免调用方改写解析器内部账本。
    # 函数用途: 取得当前流已经完整闭合、可以用于下一轮回放的所有 assistant 块。
    def completed_blocks(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(block) for block in self._completed)

    # LLM: 每个 content_block_stop 至多产出一个白名单块；未知类型必须静默丢弃，不能透传输出专用字段。
    # 函数用途: 吃入一条 SSE 事件，并在某个内容块闭合时返回整理后的完整块。
    def consume(self, event_type: str, obj: dict[str, Any]) -> dict[str, Any] | None:
        if event_type == "content_block_start":
            self._on_start(obj)
            return None
        if event_type == "content_block_delta":
            self._on_delta(obj)
            return None
        if event_type == "content_block_stop":
            return self._on_stop(obj)
        return None

    # LLM: start 事件只建立受支持块的最小输入形态，任何响应侧附加字段都不能进入历史。
    # 函数用途: 根据 content_block_start 初始化当前块及其增量缓冲。
    def _on_start(self, obj: dict[str, Any]) -> None:
        raw = obj.get("content_block")
        self._index = None
        self._block = None
        self._input_buffer = ""
        if not isinstance(raw, dict):
            return
        block_type = str(raw.get("type") or "")
        if block_type == "text":
            block = {"type": "text", "text": str(raw.get("text") or "")}
        elif block_type == "thinking":
            block = {"type": "thinking", "thinking": str(raw.get("thinking") or "")}
            signature = raw.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = signature
        elif block_type == "redacted_thinking":
            data = raw.get("data")
            if not isinstance(data, str) or not data:
                return
            block = {"type": "redacted_thinking", "data": data}
        elif block_type == "tool_use":
            tool_input = raw.get("input")
            block = {
                "type": "tool_use",
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or ""),
                "input": tool_input if isinstance(tool_input, dict) else {},
            }
        else:
            return
        try:
            self._index = int(obj.get("index"))
        except (TypeError, ValueError):
            return
        self._block = block

    # LLM: thinking/signature 仅写内部块，text_delta 仍由主解析器单独发给 on_chunk，工具参数保持 JSON 字符串直到 stop。
    # 函数用途: 把当前块对应的增量内容追加到缓冲中。
    def _on_delta(self, obj: dict[str, Any]) -> None:
        if self._block is None or obj.get("index") != self._index:
            return
        delta = obj.get("delta")
        if not isinstance(delta, dict):
            return
        delta_type = str(delta.get("type") or "")
        block_type = self._block.get("type")
        if block_type == "text" and delta_type == "text_delta":
            self._block["text"] += str(delta.get("text") or "")
        elif block_type == "thinking" and delta_type == "thinking_delta":
            self._block["thinking"] += str(delta.get("thinking") or "")
        elif block_type == "thinking" and delta_type == "signature_delta":
            self._block["signature"] = str(self._block.get("signature") or "") + str(
                delta.get("signature") or ""
            )
        elif block_type == "tool_use" and delta_type == "input_json_delta":
            self._input_buffer += str(delta.get("partial_json") or "")

# LLM: tool_use 的 input 必须以已解析 dict 回放；损坏 JSON 不生成工具块，整轮终态由工具累积器保留。
    # 函数用途: 在 content_block_stop 时闭合当前块、记录顺序并返回它。
    def _on_stop(self, obj: dict[str, Any]) -> dict[str, Any] | None:
        if self._block is None or obj.get("index") != self._index:
            return None
        block = self._block
        if block.get("type") == "tool_use" and self._input_buffer.strip():
            parsed, parse_failed = _parse_tool_input(self._input_buffer)
            if parse_failed:
                self._index, self._block, self._input_buffer = None, None, ""
                return None
            block["input"] = parsed
        self._completed.append(block)
        self._index = None
        self._block = None
        self._input_buffer = ""
        return block


# LLM: 调用方必须检查失败标记；空增量合法，但损坏 JSON/数组/标量绝不是空对象调用。
# 函数用途: 校验工具 JSON 的对象形态，不修复或推测参数。
def _parse_tool_input(buffer: str) -> tuple[dict[str, Any], bool]:
    text = buffer.strip()
    if not text:
        return {}, False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}, True
    return (parsed, False) if isinstance(parsed, dict) else ({}, True)


def json_object_or_none(line: str) -> dict[str, Any] | None:
    """Parse one SSE data line as a JSON object, ignoring malformed keepalives."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _anthropic_usage(obj: dict[str, Any], event_type: str) -> dict[str, Any]:
    if event_type == "message_start":
        message = obj.get("message", {})
        return _usage_dict(message.get("usage")) if isinstance(message, dict) else {}
    return _usage_dict(obj.get("usage"))


def _usage_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "StreamCompletion",
    "StreamEvent",
    "anthropic_stream_contents",
    "anthropic_stream_events",
    "json_object_or_none",
    "openai_stream_contents",
    "openai_stream_events",
]
