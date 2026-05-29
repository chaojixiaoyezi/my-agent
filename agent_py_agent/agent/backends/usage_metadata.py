# LLM: Provider usage helpers keep backend adapters thin while preserving stream usage chunks.
# 模块用途: 收集模型流式文本和 usage 元数据；不发请求、不判断业务状态。

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .stream_parsers import anthropic_stream_events, openai_stream_events


# LLM: usage_dict normalizes provider usage payloads without inventing missing counts.
# 函数用途: 把模型返回的 usage 字段安全转成 dict；非 dict 值一律当作没有 usage。
def usage_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


# LLM: merge_usage overlays later stream usage updates onto earlier usage fragments.
# 函数用途: 合并流式响应里的 usage 片段；后到字段覆盖同名旧字段。
def merge_usage(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update(update)
    return merged


# LLM: openai_stream_payload asks compatible providers to include final stream usage.
# 函数用途: 在不改变业务请求语义的前提下，为 OpenAI 兼容流式请求补 include_usage。
def openai_stream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    patched = dict(payload)
    stream_options = patched.get("stream_options")
    patched["stream_options"] = (
        {**stream_options, "include_usage": True}
        if isinstance(stream_options, dict)
        else {"include_usage": True}
    )
    return patched


# LLM: collect_openai_stream returns both accumulated text and provider usage metadata.
# 函数用途: 收集 OpenAI 兼容 SSE 文本块，同时保留最终 usage，供 compact 预算优先使用真实值。
def collect_openai_stream(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    return _collect_stream_events(openai_stream_events(lines), on_chunk)


# LLM: collect_anthropic_stream returns both accumulated text and Anthropic usage updates.
# 函数用途: 收集 Anthropic 兼容 SSE 文本块，同时合并 message_start/message_delta 里的 usage。
def collect_anthropic_stream(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    return _collect_stream_events(anthropic_stream_events(lines), on_chunk)


# LLM: _collect_stream_events is the shared event accumulator for stream adapters.
# 函数用途: 遍历标准化 stream event，拼接文本并合并 usage；不做 provider 专项判断。
def _collect_stream_events(
    events: Iterable[object],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    usage: dict[str, Any] = {}
    for event in events:
        event_usage = getattr(event, "usage", None)
        if event_usage:
            usage = merge_usage(usage, event_usage)
        content = str(getattr(event, "content", "") or "")
        if not content:
            continue
        parts.append(content)
        _emit_chunk(on_chunk, content)
    return "".join(parts), usage


# LLM: _emit_chunk preserves existing streaming callbacks while centralizing checks.
# 函数用途: 如果调用方提供回调，就把每个流式文本块原样推给回调。
def _emit_chunk(on_chunk: Callable[[str], None] | None, content: str) -> None:
    if on_chunk is not None:
        on_chunk(content)


__all__ = [
    "collect_anthropic_stream",
    "collect_openai_stream",
    "merge_usage",
    "openai_stream_payload",
    "usage_dict",
]
