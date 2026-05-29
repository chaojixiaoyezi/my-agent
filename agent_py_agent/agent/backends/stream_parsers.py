# LLM: Model backend module; keep streaming, gateway, and backend protocol shapes stable.
# 模块用途: 封装模型后端协议、流式解析和 gateway 辅助调用。

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


# LLM: StreamEvent is the normalized streaming unit shared by provider adapters.
# 类用途: 保存一个流式事件里的可见文本和可选 usage 元数据，避免后端各自解析预算。
@dataclass(frozen=True)
class StreamEvent:
    """One normalized stream event with optional visible text and usage metadata."""

    content: str = ""
    usage: dict[str, Any] | None = None


# LLM: openai_stream_contents 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理openai流式contents相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def openai_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    for event in openai_stream_events(lines):
        if event.content:
            yield event.content


# LLM: openai_stream_events preserves usage chunks while keeping visible text streaming.
# 函数用途: 解析 OpenAI-compatible SSE，逐段产出文本，同时保留最终 usage chunk。
def openai_stream_events(lines: Iterable[str]) -> Iterator[StreamEvent]:
    for line in lines:
        if line == "[DONE]":
            break
        obj = json_object_or_none(line)
        if obj is None:
            continue
        choices = obj.get("choices", [])
        content = choices[0].get("delta", {}).get("content") if choices else None
        usage = _usage_dict(obj.get("usage"))
        if content or usage:
            yield StreamEvent(content=str(content or ""), usage=usage or None)


# LLM: anthropic_stream_contents 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理anthropic流式contents相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def anthropic_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    for event in anthropic_stream_events(lines):
        if event.content:
            yield event.content


# LLM: anthropic_stream_events preserves message_start/message_delta usage without delaying text chunks.
# 函数用途: 解析 Anthropic-compatible SSE，保留 usage 事件并继续逐段流式输出正文。
def anthropic_stream_events(lines: Iterable[str]) -> Iterator[StreamEvent]:
    for line in lines:
        obj = json_object_or_none(line)
        if obj is None:
            continue
        event_type = obj.get("type", "")
        if event_type == "message_stop":
            break
        text = obj.get("delta", {}).get("text", "") if event_type == "content_block_delta" else ""
        usage = _anthropic_usage(obj, event_type)
        if text or usage:
            yield StreamEvent(content=str(text or ""), usage=usage or None)


# LLM: json_object_or_none 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理JSONobjectnone相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
def json_object_or_none(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# LLM: _anthropic_usage extracts usage from Anthropic event shapes without delaying text.
# 函数用途: 从 message_start 或 message_delta 等事件里取 usage；没有 usage 时返回空 dict。
def _anthropic_usage(obj: dict[str, Any], event_type: str) -> dict[str, Any]:
    if event_type == "message_start":
        message = obj.get("message", {})
        return _usage_dict(message.get("usage")) if isinstance(message, dict) else {}
    return _usage_dict(obj.get("usage"))


# LLM: _usage_dict normalizes optional provider usage metadata for stream events.
# 函数用途: 只接受 dict 形态的 usage，避免字符串或空值污染模型预算统计。
def _usage_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "StreamEvent",
    "anthropic_stream_contents",
    "anthropic_stream_events",
    "json_object_or_none",
    "openai_stream_contents",
    "openai_stream_events",
]
