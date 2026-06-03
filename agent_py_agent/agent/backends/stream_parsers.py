
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """One normalized stream event with optional visible text and usage metadata."""

    content: str = ""
    usage: dict[str, Any] | None = None


def openai_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    """Yield visible text chunks from OpenAI-compatible SSE data lines."""
    for event in openai_stream_events(lines):
        if event.content:
            yield event.content


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


def anthropic_stream_contents(lines: Iterable[str]) -> Iterator[str]:
    """Yield visible text chunks from Anthropic-compatible SSE data lines."""
    for event in anthropic_stream_events(lines):
        if event.content:
            yield event.content


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
    "StreamEvent",
    "anthropic_stream_contents",
    "anthropic_stream_events",
    "json_object_or_none",
    "openai_stream_contents",
    "openai_stream_events",
]
