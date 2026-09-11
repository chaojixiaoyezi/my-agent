# LLM: 消费 typed SSE delta；禁止依据正文前缀去重或猜测 snapshot，展示与返回正文必须逐字一致。
# 模块用途: 汇总模型的流式正文、思考、工具块和用量，保留原始顺序及停止边界。
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from ..conversation.tool_input_progress import TOOL_INPUT_PROGRESS_SCHEMA
from .stream_parsers import (
    StreamCompletion,
    anthropic_stream_events,
    openai_stream_events,
)

_TOOL_INPUT_PROGRESS_FLUSH_SECONDS = 1.0
_TOOL_INPUT_PROGRESS_FLUSH_CHARS = 8_192


def usage_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def merge_usage(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update(update)
    return merged


def openai_stream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    patched = dict(payload)
    stream_options = patched.get("stream_options")
    patched["stream_options"] = (
        {**stream_options, "include_usage": True}
        if isinstance(stream_options, dict)
        else {"include_usage": True}
    )
    return patched


def collect_openai_stream(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    text, usage, _blocks, _completion = collect_openai_stream_with_completion(lines, on_chunk)
    return text, usage


# LLM: OpenAI delta.content 是增量；回调和最终正文使用同一份原文，不能据重复字符改变协议。
# 函数用途: 收集一次 OpenAI 流并完成思考展示，返回正文、工具、用量和停止事实。
def collect_openai_stream_with_completion(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
    on_thinking_delta: Callable[[str], None] | None = None,
    on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
    """收集 OpenAI-compatible SSE，并把 reasoning_content 与正文分流。

    支持 ``complete(text)`` 的观察器会在第一段正文/工具块之前或流结束时收到一次
    思考终态；普通 callback 只接收增量。任何展示回调异常都不能中断模型响应。
    """
    parts: list[str] = []
    usage: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    thinking_parts: list[str] = []
    thinking_closed = False
    thinking_complete = getattr(on_thinking_delta, "complete", None)
    tool_input_emitter = _ToolInputProgressEmitter(on_tool_input_progress)
    events = openai_stream_events(lines)
    completion = StreamCompletion()
    while True:
        event, completion, done = _next_stream_event(events, completion)
        if done:
            break
        progress = getattr(event, "tool_input_progress", None)
        if progress is not None:
            thinking_closed = _complete_openai_thinking(
                thinking_parts, thinking_complete, already_closed=thinking_closed,
            )
            tool_input_emitter.accept(progress)
        block = getattr(event, "tool_use_block", None)
        if block is not None:
            thinking_closed = _complete_openai_thinking(
                thinking_parts,
                thinking_complete,
                already_closed=thinking_closed,
            )
            blocks.append(block)
            continue
        event_usage = getattr(event, "usage", None)
        if event_usage:
            usage = merge_usage(usage, event_usage)
        thinking = str(getattr(event, "thinking_content", "") or "")
        if thinking:
            thinking_parts.append(thinking)
            if callable(on_thinking_delta):
                try:
                    on_thinking_delta(thinking)
                except Exception:
                    pass
            continue
        content = str(getattr(event, "content", "") or "")
        if not content:
            continue
        thinking_closed = _complete_openai_thinking(
            thinking_parts,
            thinking_complete,
            already_closed=thinking_closed,
        )
        parts.append(content)
        _emit_chunk(on_chunk, content)
    _complete_openai_thinking(
        thinking_parts,
        thinking_complete,
        already_closed=thinking_closed,
    )
    return "".join(parts), usage, blocks, completion


# LLM: Chat Completions 没有统一 reasoning block-stop；只允许在正文/工具开始或流结束的
# typed 边界封口一次，callback 异常不能污染 provider 结果。
# 函数用途: 把累计的 OpenAI-compatible 思考文本结束成一块用户可见历史。
def _complete_openai_thinking(
    parts: list[str],
    complete: object,
    *,
    already_closed: bool,
) -> bool:
    if already_closed or not parts:
        return already_closed
    if callable(complete):
        try:
            complete("".join(parts))
        except Exception:
            pass
    return True


def collect_anthropic_stream(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    text, usage, _blocks = collect_anthropic_stream_with_tools(lines, on_chunk)
    return text, usage


def collect_anthropic_stream_with_tools(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Collect Anthropic SSE into (text, usage, tool_use_blocks).

    Native tool_use path: streamed tool_use blocks are accumulated into the
    third return value while text/usage collection stays identical to the
    text-protocol path.
    """
    text, usage, blocks, _completion = collect_anthropic_stream_with_completion(lines, on_chunk)
    return text, usage, blocks


# LLM: 该收集器是 Anthropic 文本/思考/工具块与脱敏参数进度的分流边界；
# thinking complete 必须在原 content_block_stop 处先于后续正文发布，text_delta 逐字追加，不猜累计快照。
# 函数用途: 收集一次 Anthropic SSE 的正文、用量、工具块和完整性，并按块边界发送思考终态与展示计数。
def collect_anthropic_stream_with_completion(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
    on_thinking_delta: Callable[[str], None] | None = None,
    on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
    """Like ``collect_anthropic_stream_with_tools`` but also returns the stream's
    ``StreamCompletion`` health check (truncation detection for the native path).

    The fourth value captures the generator's return value (whether ``message_stop``
    was seen, the final ``stop_reason``, and whether a tool_use JSON buffer was left
    open). Text/3-tuple callers are unaffected.
    ``on_thinking_delta`` receives live thinking deltas for rich transcript sinks;
    text/usage callers ignore it.
    A structured ``on_thinking_delta`` observer may expose ``complete(text)``;
    it receives the completed block at the exact ``content_block_stop`` boundary,
    before any later text block is observed. Plain callback compatibility remains.
    ``on_tool_input_progress`` receives only tool name/index/phase/cumulative
    character counters, batched before leaving this collector.
    """
    parts: list[str] = []
    usage: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    tool_input_emitter = _ToolInputProgressEmitter(on_tool_input_progress)
    thinking_complete = getattr(on_thinking_delta, "complete", None)
    events = anthropic_stream_events(lines)
    completion = StreamCompletion()
    while True:
        event, completion, done = _next_stream_event(events, completion)
        if done:
            break
        progress = getattr(event, "tool_input_progress", None)
        if progress is not None:
            tool_input_emitter.accept(progress)
        block = getattr(event, "tool_use_block", None)
        if block is not None:
            blocks.append(block)
            continue
        assistant_block = getattr(event, "assistant_content_block", None)
        if (
            isinstance(assistant_block, dict)
            and assistant_block.get("type") == "thinking"
            and callable(thinking_complete)
        ):
            thinking_text = str(assistant_block.get("thinking") or "")
            if thinking_text:
                try:
                    thinking_complete(thinking_text)
                except Exception:
                    pass
        event_usage = getattr(event, "usage", None)
        if event_usage:
            usage = merge_usage(usage, event_usage)
        thinking = str(getattr(event, "thinking_content", "") or "")
        if thinking and callable(on_thinking_delta):
            on_thinking_delta(thinking)
            continue
        content = str(getattr(event, "content", "") or "")
        if not content:
            continue
        parts.append(content)
        _emit_chunk(on_chunk, content)
    return "".join(parts), usage, blocks, completion


# LLM: 该对象只抑制过密的展示回调，不能吞掉 started/ready，也不能把原始
# partial JSON 放进状态；显示 callback 的异常不得改变真实模型响应。
# 类用途: 将 Anthropic 工具参数逐 delta 计数合成首条、每秒/每 8K 和收口进度。
class _ToolInputProgressEmitter:
    # LLM: callback 是可选 UI observer；所有节流状态按一次物理响应收口，不能
    # 持久化或跨模型请求复用。
    # 函数用途: 为一次流式响应初始化工具参数展示节流器。
    def __init__(
        self,
        callback: Callable[[dict[str, object]], None] | None,
    ) -> None:
        self.callback = callback if callable(callback) else None
        self._last_chars: dict[int, int] = {}
        self._last_emitted_at: dict[int, float] = {}

    # LLM: accept 只依据 typed phase/count/time 决定是否显示；任何 observer
    # 失败都必须 fail-open，让完整 tool_use 继续由 parser 正常返回。
    # 函数用途: 接收一条计数快照，并在达到合批边界时安全通知界面。
    def accept(self, value: object) -> None:
        if self.callback is None or not isinstance(value, dict):
            return
        phase = str(value.get("phase") or "").strip().lower()
        try:
            stream_index = max(0, int(value.get("stream_index") or 0))
            received_chars = max(0, int(value.get("received_chars") or 0))
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        previous_chars = self._last_chars.get(stream_index, 0)
        previous_at = self._last_emitted_at.get(stream_index, 0.0)
        should_emit = (
            phase in {"started", "ready"}
            or received_chars - previous_chars >= _TOOL_INPUT_PROGRESS_FLUSH_CHARS
            or now - previous_at >= _TOOL_INPUT_PROGRESS_FLUSH_SECONDS
        )
        if not should_emit:
            return
        public = {
            "schema": TOOL_INPUT_PROGRESS_SCHEMA,
            "phase": phase,
            "stream_index": stream_index,
            "tool": str(value.get("tool") or "Tool")[:80],
            "received_chars": received_chars,
        }
        try:
            self.callback(public)
        except Exception:
            return
        self._last_chars[stream_index] = received_chars
        self._last_emitted_at[stream_index] = now


def _next_stream_event(events: Any, completion: StreamCompletion) -> tuple[Any, StreamCompletion, bool]:
    """Pull the next event; on exhaustion capture the generator's StreamCompletion return value.

    Returns ``(event, completion, done)``: when ``done`` is True the iterator is exhausted and
    ``completion`` reflects the stream's truncation health check (or the prior value as fallback).
    """
    try:
        return next(events), completion, False
    except StopIteration as stop:
        value = stop.value
        return None, value if isinstance(value, StreamCompletion) else completion, True


def _emit_chunk(on_chunk: Callable[[str], None] | None, content: str) -> None:
    if on_chunk is not None:
        on_chunk(content)


__all__ = [
    "collect_anthropic_stream",
    "collect_anthropic_stream_with_completion",
    "collect_anthropic_stream_with_tools",
    "collect_openai_stream",
    "collect_openai_stream_with_completion",
    "merge_usage",
    "openai_stream_payload",
    "usage_dict",
]
