
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


# 截断检测的常量:Anthropic message_delta.stop_reason 取这些值时，表示模型在写完整
# 工具参数 JSON 之前就被 token 上限/长度上限切断（MiniMax 长 content native 写入的主因）。
_TRUNCATING_STOP_REASONS = frozenset({"max_tokens", "length"})


# LLM: 这是流结束的结构化事实；assistant_content_blocks 不直接作为可见 chunk，显式 rich transcript 只能由上层收尾筛出 type=thinking 正文。
# 类用途: 汇总一条流是否完整、停止原因，以及下一轮请求需要原样续接的有序 assistant 块。
@dataclass(frozen=True)
class StreamCompletion:
    """Anthropic SSE 流是否正常收尾的体检结果（``anthropic_stream_events`` 的 return 值）。

    native 截断检测专用：流在 ``message_stop`` 之前就 EOF（代理/CDN 截断、服务端冲完
    部分缓冲就断），或 ``message_delta.stop_reason`` ∈ {max_tokens,length} 且仍有未闭合的
    tool_use 参数缓冲 → 这一帧 tool_use 的参数 JSON 是半截的。text 协议消费者忽略此值，
    行为零变化。``truncated`` 把这两类信号收成一个布尔，交给 ModelResponse 带出。
    """

    saw_message_stop: bool = False
    stop_reason: str = ""
    open_tool_buffer: bool = False
    assistant_content_blocks: tuple[dict[str, Any], ...] = ()

    @property
    def truncated(self) -> bool:
        if self.open_tool_buffer:
            return True
        # 流在 message_stop / stop_reason 之前就 EOF：半截响应（含被切断的 tool-call JSON）
        # 绝不能当完整成功（对照 claw client.py chat_stream 的 not(saw_stop or stop_reason)）。
        return not (self.saw_message_stop or self.stop_reason)


def openai_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    """Yield visible text chunks from OpenAI-compatible SSE data lines."""
    for event in openai_stream_events(lines):
        if event.content:
            yield event.content


def openai_stream_events(lines: Iterable[str]) -> Iterator[StreamEvent]:
    tool_acc = _OpenAIToolCallAccumulator()
    reasoning_parts: list[str] = []
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
        tool_acc.consume(delta.get("tool_calls"))
        content = delta.get("content")
        reasoning = delta.get("reasoning_content")
        finish_reason = str(choice.get("finish_reason") or "") or finish_reason
        usage = _usage_dict(obj.get("usage"))
        if isinstance(reasoning, str) and reasoning:
            reasoning_parts.append(reasoning)
            yield StreamEvent(thinking_content=reasoning)
        if content or usage:
            yield StreamEvent(content=str(content or ""), usage=usage or None)
    blocks, parse_failed = tool_acc.finish()
    for block in blocks:
        yield StreamEvent(tool_use_block=block)
    return StreamCompletion(
        saw_message_stop=saw_done,
        stop_reason=finish_reason,
        open_tool_buffer=parse_failed,
        assistant_content_blocks=(
            ({"type": "thinking", "thinking": "".join(reasoning_parts)},)
            if reasoning_parts
            else ()
        ),
    )


class _OpenAIToolCallAccumulator:
    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def consume(self, raw_calls: object) -> None:
        if not isinstance(raw_calls, list):
            return
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("index", 0) or 0)
            except (TypeError, ValueError):
                continue
            call = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call["id"] += str(raw.get("id") or "")
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            call["name"] += str(function.get("name") or "")
            call["arguments"] += str(function.get("arguments") or "")

    def finish(self) -> tuple[list[dict[str, Any]], bool]:
        blocks: list[dict[str, Any]] = []
        parse_failed = False
        for index in sorted(self._calls):
            call = self._calls[index]
            try:
                parsed = json.loads(call["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                parsed = {}
                parse_failed = True
            if not isinstance(parsed, dict):
                parsed = {}
                parse_failed = True
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


class _AnthropicToolUseAccumulator:
    """Accumulates Anthropic streamed tool_use blocks across SSE events.

    A tool_use block arrives as ``content_block_start`` (carries id/name),
    one or more ``content_block_delta`` with ``input_json_delta.partial_json``
    fragments, then ``content_block_stop``. We buffer the partial JSON and
    parse it on stop, returning the finished ``{"id","name","input"}`` block.
    """

    def __init__(self) -> None:
        self._index: int | None = None
        self._id: str = ""
        self._name: str = ""
        self._buffer: str = ""
        self._last_parse_failed: bool = False

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

    def _on_start(self, obj: dict[str, Any]) -> None:
        block = obj.get("content_block")
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            self._index = None
            return
        self._index = obj.get("index")
        self._id = str(block.get("id", "") or "")
        self._name = str(block.get("name", "") or "")
        self._buffer = ""
        self._last_parse_failed = False  # 新 tool_use 块开始：清掉上一块的截断标记

    def _on_delta(self, obj: dict[str, Any]) -> None:
        if self._index is None:
            return
        delta = obj.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
            self._buffer += str(delta.get("partial_json", "") or "")

    def _on_stop(self, obj: dict[str, Any]) -> dict[str, Any] | None:
        if self._index is None or obj.get("index") != self._index:
            return None
        parsed, parse_failed = _parse_tool_input(self._buffer)
        self._last_parse_failed = parse_failed
        block = {"id": self._id, "name": self._name, "input": parsed}
        self._index = None
        self._buffer = ""
        return block


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

    # LLM: tool_use 的 input 必须以已解析 dict 回放；损坏 JSON 留给既有截断检测处理，不能把半截字符串放进下一轮请求。
    # 函数用途: 在 content_block_stop 时闭合当前块、记录顺序并返回它。
    def _on_stop(self, obj: dict[str, Any]) -> dict[str, Any] | None:
        if self._block is None or obj.get("index") != self._index:
            return None
        block = self._block
        if block.get("type") == "tool_use" and self._input_buffer.strip():
            parsed, _parse_failed = _parse_tool_input(self._input_buffer)
            block["input"] = parsed
        self._completed.append(block)
        self._index = None
        self._block = None
        self._input_buffer = ""
        return block


def _parse_tool_input(buffer: str) -> tuple[dict[str, Any], bool]:
    """Parse a tool_use input-JSON buffer; return ``(input_dict, parse_failed)``.

    ``parse_failed`` distinguishes a *truncated/broken* JSON buffer (non-empty text that
    fails ``json.loads`` → likely cut off mid-stream) from a legitimately empty buffer.
    The returned dict is always a real dict so downstream block consumers stay unchanged;
    truncation is signalled out-of-band via this flag (not by mutating the block).
    """
    text = buffer.strip()
    if not text:
        return {}, False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}, True  # 非空但解析失败 = 半截 JSON（截断特征），标记带出但仍回空 dict
    return (parsed if isinstance(parsed, dict) else {}), False


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
