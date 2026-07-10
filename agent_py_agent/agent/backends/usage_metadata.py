
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .stream_parsers import (
    StreamCompletion,
    anthropic_stream_events,
    openai_stream_events,
)


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


def collect_openai_stream_with_completion(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
    parts: list[str] = []
    accumulated = ""
    previous_raw = ""
    usage: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    events = openai_stream_events(lines)
    completion = StreamCompletion()
    while True:
        event, completion, done = _next_stream_event(events, completion)
        if done:
            break
        block = getattr(event, "tool_use_block", None)
        if block is not None:
            blocks.append(block)
            continue
        event_usage = getattr(event, "usage", None)
        if event_usage:
            usage = merge_usage(usage, event_usage)
        content = str(getattr(event, "content", "") or "")
        if not content:
            continue
        chunk = _stream_delta(previous_raw, content)
        previous_raw = content
        if not chunk:
            continue
        parts.append(chunk)
        accumulated += chunk
        _emit_chunk(on_chunk, chunk)
    return accumulated, usage, blocks, completion


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


def collect_anthropic_stream_with_completion(
    lines: Iterable[str],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
    """Like ``collect_anthropic_stream_with_tools`` but also returns the stream's
    ``StreamCompletion`` health check (truncation detection for the native path).

    The fourth value captures the generator's return value (whether ``message_stop``
    was seen, the final ``stop_reason``, and whether a tool_use JSON buffer was left
    open). Text/3-tuple callers are unaffected.
    """
    parts: list[str] = []
    accumulated = ""
    previous_raw = ""
    usage: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    events = anthropic_stream_events(lines)
    completion = StreamCompletion()
    while True:
        event, completion, done = _next_stream_event(events, completion)
        if done:
            break
        block = getattr(event, "tool_use_block", None)
        if block is not None:
            blocks.append(block)
            continue
        event_usage = getattr(event, "usage", None)
        if event_usage:
            usage = merge_usage(usage, event_usage)
        content = str(getattr(event, "content", "") or "")
        if not content:
            continue
        chunk = _stream_delta(previous_raw, content)
        previous_raw = content
        if not chunk:
            continue
        parts.append(chunk)
        accumulated += chunk
        _emit_chunk(on_chunk, chunk)
    return accumulated, usage, blocks, completion


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


def _collect_stream_events(
    events: Iterable[object],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    accumulated = ""
    previous_raw = ""
    usage: dict[str, Any] = {}
    for event in events:
        event_usage = getattr(event, "usage", None)
        if event_usage:
            usage = merge_usage(usage, event_usage)
        content = str(getattr(event, "content", "") or "")
        if not content:
            continue
        chunk = _stream_delta(previous_raw, content)
        previous_raw = content
        if not chunk:
            continue
        parts.append(chunk)
        accumulated += chunk
        _emit_chunk(on_chunk, chunk)
    return accumulated, usage


def _stream_delta(previous_raw: str, content: str) -> str:
    if previous_raw and len(content) > len(previous_raw) and content.startswith(previous_raw):
        return content[len(previous_raw) :]
    return content


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
