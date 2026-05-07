from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


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


def json_object_or_none(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["anthropic_stream_contents", "json_object_or_none", "openai_stream_contents"]
