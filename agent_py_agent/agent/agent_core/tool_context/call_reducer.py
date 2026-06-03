
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssistantToolRoundContextRequest:
    response_text: str
    tool_calls: list[dict[str, Any]]
    max_inline_chars: int = 4000
    max_value_preview_chars: int = 240


def render_assistant_tool_round_context(request: AssistantToolRoundContextRequest) -> str:
    if not _should_reduce_tool_round(request):
        return request.response_text
    lines = [
        "[assistant tool-call response summarized]",
        f"- original_response_chars: {len(request.response_text)}",
        f"- tool_call_count: {len(request.tool_calls)}",
    ]
    lead = _non_tool_text_preview(request.response_text, request.max_value_preview_chars)
    if lead:
        lines.append(f"- non_tool_text_preview: {lead}")
    for index, payload in enumerate(request.tool_calls, start=1):
        lines.extend(_tool_call_summary_lines(index, payload, request.max_value_preview_chars))
    return "\n".join(lines)


def render_tool_payload_for_live_prompt(
    payload: object, *, max_inline_chars: int = 4000, max_value_preview_chars: int = 240
) -> str:
    if not isinstance(payload, dict):
        return _bounded_repr(payload, max_inline_chars, max_value_preview_chars)
    return "\n".join(_tool_call_summary_lines(1, payload, max_value_preview_chars))


def _should_reduce_tool_round(request: AssistantToolRoundContextRequest) -> bool:
    if len(request.response_text) > request.max_inline_chars:
        return True
    return any(
        _value_size(value) > request.max_value_preview_chars * 4
        for payload in request.tool_calls
        for key, value in payload.items()
        if key != "tool"
    )


def _tool_call_summary_lines(index: int, payload: dict[str, Any], max_preview_chars: int) -> list[str]:
    tool = str(payload.get("tool") or "unknown")
    lines = [f"- tool_call_{index}: tool={tool}"]
    for key, value in payload.items():
        if key == "tool":
            continue
        lines.append(f"  - {key}: {_value_summary(value, max_preview_chars)}")
    return lines


def _bounded_repr(value: object, max_inline_chars: int, max_preview_chars: int) -> str:
    text = repr(value)
    if len(text) <= max_inline_chars:
        return text
    return _large_text_summary(text, max_preview_chars)


def _value_summary(value: Any, max_preview_chars: int) -> str:
    if isinstance(value, str):
        if len(value) <= max_preview_chars:
            return value
        return _large_text_summary(value, max_preview_chars)
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(text) <= max_preview_chars:
            return text
        return _large_text_summary(text, max_preview_chars)
    return repr(value)


def _large_text_summary(text: str, max_preview_chars: int) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    preview = text[:max_preview_chars].replace("\n", "\\n")
    return (
        f"<large text omitted chars={len(text)} bytes={len(text.encode('utf-8'))} "
        f"sha256={digest} preview={preview}>"
    )


def _non_tool_text_preview(text: str, max_preview_chars: int) -> str:
    without_calls = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", " ", text, flags=re.DOTALL)
    compact = " ".join(without_calls.split())
    return compact[:max_preview_chars]


def _value_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except TypeError:
        return len(repr(value))
