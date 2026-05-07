# LLM: Model backend module; keep streaming, gateway, and backend protocol shapes stable.
# 模块用途: 封装模型后端协议、流式解析和 gateway 辅助调用。

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


# LLM: openai_stream_contents 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理openai流式contents相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def openai_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        if line == "[DONE]":
            break
        obj = json_object_or_none(line)
        if obj is None:
            continue
        choices = obj.get("choices", [])
        content = choices[0].get("delta", {}).get("content") if choices else None
        if content:
            yield content


# LLM: anthropic_stream_contents 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理anthropic流式contents相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def anthropic_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        obj = json_object_or_none(line)
        if obj is None:
            continue
        event_type = obj.get("type", "")
        if event_type == "message_stop":
            break
        text = obj.get("delta", {}).get("text", "") if event_type == "content_block_delta" else ""
        if text:
            yield text


# LLM: json_object_or_none 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理JSONobjectnone相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
def json_object_or_none(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["anthropic_stream_contents", "json_object_or_none", "openai_stream_contents"]
