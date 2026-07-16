from __future__ import annotations

"""Keep model/runtime protocols out of text delivered to users.

The model-facing tool protocol has more than one wire spelling.  Besides the
canonical bracket/native forms, open models can downgrade tool calls to XML
or emit a tool name as the XML element itself.  Every user-facing channel must
use this module instead of maintaining a channel-specific deny list.
"""

import re
from dataclasses import dataclass

_INTERNAL_LITERAL_TOKENS = (
    "[natural-user-reply]",
    "[main_agent_",
    "[/main_agent_",
    "[run_",
    "[/run_",
    "[subagent_",
    "[/subagent_",
    "[tool_call",
    "[/tool_call",
    "[tool_result",
    "[/tool_result",
    "<tool_call",
    "</tool_call",
    "<tool_result",
    "</tool_result",
    "<function_call",
    "</function_call",
    "<function_response",
    "</function_response",
    "<minimax:tool_call",
    "</minimax:tool_call",
)

_BRACKET_TOOL_BLOCK_RE = re.compile(
    r"\[TOOL_(?:CALL|RESULT)\].*?(?:\[/TOOL_(?:CALL|RESULT)\]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_FENCED_TOOL_BLOCK_RE = re.compile(
    r"^[ \t]*(?:`{3,}|~{3,})[ \t]*"
    r"(?:tool[ _-]*(?:calls?|results?|outputs?)|function[ _-]*(?:calls?|responses?))"
    r"[^\r\n]*(?:\r?\n|\Z).*?"
    r"(?:^[ \t]*(?:`{3,}|~{3,})[ \t]*(?:\r?\n|\Z)|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_XML_TOOL_BLOCK_RE = re.compile(
    r"<(?P<tag>tool_calls?|tool_results?|function_calls?|function_responses?|function)\b[^>]*>"
    r".*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_UNCLOSED_XML_TOOL_BLOCK_RE = re.compile(
    r"<(?:tool_calls?|tool_results?|function_calls?|function_responses?)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_BARE_FUNCTION_BLOCK_RE = re.compile(
    r"<function\s*=\s*['\"]?[A-Za-z_][A-Za-z0-9_.:-]{0,119}['\"]?\s*>"
    r".*?(?:</function\s*>|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_PLAIN_TEXT_TOOL_LINE_RE = re.compile(
    r"(?im)^[ \t]*\[tool:[A-Za-z_][A-Za-z0-9_.:-]{0,119}\][^\r\n]*(?:\r?\n|\Z)"
)
_DIRECT_XML_TOOL_OPEN_RE = re.compile(
    r"(?im)^[ \t]*<(?P<tag>[a-z][a-z0-9]*(?:[_:][a-z0-9]+)+)\b[^>]*>"
)


@dataclass(frozen=True)
class UserVisibleTextSanitization:
    content: str
    removed_protocol: bool = False


def sanitize_user_visible_text(content: object) -> UserVisibleTextSanitization:
    """Remove internal/tool envelopes while preserving surrounding model prose."""
    text = str(content or "").strip()
    if not text:
        return UserVisibleTextSanitization(content="")

    cleaned = text
    removed = False
    for pattern in (
        _FENCED_TOOL_BLOCK_RE,
        _BRACKET_TOOL_BLOCK_RE,
        _XML_TOOL_BLOCK_RE,
        _UNCLOSED_XML_TOOL_BLOCK_RE,
        _BARE_FUNCTION_BLOCK_RE,
        _PLAIN_TEXT_TOOL_LINE_RE,
    ):
        cleaned, count = pattern.subn("", cleaned)
        removed = removed or count > 0

    cleaned, direct_count = _strip_direct_xml_tool_blocks(cleaned)
    removed = removed or direct_count > 0

    folded = cleaned.casefold()
    positions = [folded.find(token) for token in _INTERNAL_LITERAL_TOKENS]
    positions = [position for position in positions if position >= 0]
    if positions:
        cleaned = cleaned[: min(positions)]
        removed = True

    if removed:
        cleaned = re.sub(r"[ \t]+(?=\r?$)", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"(?:\r?\n){3,}", "\n\n", cleaned)
    return UserVisibleTextSanitization(content=cleaned.strip(), removed_protocol=removed)


def contains_internal_protocol(content: object) -> bool:
    return sanitize_user_visible_text(content).removed_protocol


def _strip_direct_xml_tool_blocks(text: str) -> tuple[str, int]:
    """Strip standalone ``<tool_name>...</tool_name>`` model downgrades.

    A colon/underscore in a line-leading element name distinguishes these
    model protocol envelopes from ordinary HTML.  A missing close tag consumes
    the remaining text so truncated tool arguments cannot reach an IM.
    """
    cleaned = text
    removed = 0
    cursor = 0
    while match := _DIRECT_XML_TOOL_OPEN_RE.search(cleaned, cursor):
        tag = match.group("tag")
        close = re.search(rf"</{re.escape(tag)}\s*>", cleaned[match.end() :], re.IGNORECASE)
        end = len(cleaned) if close is None else match.end() + close.end()
        while end < len(cleaned) and cleaned[end] in " \t\r\n":
            end += 1
        cleaned = cleaned[: match.start()] + cleaned[end:]
        removed += 1
        cursor = match.start()
    return cleaned, removed


__all__ = [
    "UserVisibleTextSanitization",
    "contains_internal_protocol",
    "sanitize_user_visible_text",
]
