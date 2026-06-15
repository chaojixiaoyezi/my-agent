
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """One normalized stream event with optional visible text and usage metadata.

    ``tool_use_block`` is populated only on the Anthropic native tool_use path,
    once a tool_use content block has finished accumulating its input JSON; it
    holds ``{"id","name","input"}``. Text/usage consumers ignore it.
    """

    content: str = ""
    usage: dict[str, Any] | None = None
    tool_use_block: dict[str, Any] | None = None


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
    tool_acc = _AnthropicToolUseAccumulator()
    for line in lines:
        obj = json_object_or_none(line)
        if obj is None:
            continue
        event_type = obj.get("type", "")
        if event_type == "message_stop":
            break
        block = tool_acc.consume(event_type, obj)
        if block is not None:
            yield StreamEvent(tool_use_block=block)
            continue
        text = obj.get("delta", {}).get("text", "") if event_type == "content_block_delta" else ""
        usage = _anthropic_usage(obj, event_type)
        if text or usage:
            yield StreamEvent(content=str(text or ""), usage=usage or None)


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

    def _on_start(self, obj: dict[str, Any]) -> None:
        block = obj.get("content_block")
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            self._index = None
            return
        self._index = obj.get("index")
        self._id = str(block.get("id", "") or "")
        self._name = str(block.get("name", "") or "")
        self._buffer = ""

    def _on_delta(self, obj: dict[str, Any]) -> None:
        if self._index is None:
            return
        delta = obj.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
            self._buffer += str(delta.get("partial_json", "") or "")

    def _on_stop(self, obj: dict[str, Any]) -> dict[str, Any] | None:
        if self._index is None or obj.get("index") != self._index:
            return None
        block = {"id": self._id, "name": self._name, "input": _parse_tool_input(self._buffer)}
        self._index = None
        self._buffer = ""
        return block


def _parse_tool_input(buffer: str) -> dict[str, Any]:
    text = buffer.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
