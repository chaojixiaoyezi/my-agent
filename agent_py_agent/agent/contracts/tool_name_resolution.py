
from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher

from ..common.value_parsing import text_value as _text

_SUGGESTION_THRESHOLD = 0.74


def resolve_dispatch_tool_name(raw_name: object, available_tools: Iterable[str] | None) -> str | None:
    """Return an executable registered tool name only when the match is deterministic."""
    raw = _text(raw_name)
    available = _available_names(available_tools)
    if not raw or not available:
        return None
    for candidate in _structured_candidates(raw):
        if candidate in available:
            return candidate
        folded = _case_insensitive_match(candidate, available)
        if folded:
            return folded
    return None


def suggested_tool_name(raw_name: object, available_tools: Iterable[str] | None) -> str:
    """Return a single close suggestion for diagnostics; callers must not execute it automatically."""
    raw = _normalized_key(_text(raw_name))
    available = _available_names(available_tools)
    if not raw or not available:
        return ""
    scored = sorted(
        (
            (SequenceMatcher(None, raw, _normalized_key(name)).ratio(), name)
            for name in available
        ),
        reverse=True,
    )
    if not scored or scored[0][0] < _SUGGESTION_THRESHOLD:
        return ""
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return ""
    return scored[0][1]


def _structured_candidates(raw: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            candidates.append(item)

    add(raw)
    normalized = raw.replace("/", ".").replace("::", ".")
    add(normalized)
    suffixless = _strip_tool_suffix(normalized)
    add(suffixless)
    segments = [segment for segment in normalized.split(".") if segment]
    for index in range(1, len(segments)):
        suffix = ".".join(segments[index:])
        add(suffix)
        add(_strip_tool_suffix(suffix))
    return candidates


def _available_names(values: Iterable[str] | None) -> set[str]:
    return {str(item).strip() for item in values or [] if str(item).strip()}


def _case_insensitive_match(value: str, available: set[str]) -> str:
    matches = [name for name in available if name.lower() == value.lower()]
    return matches[0] if len(matches) == 1 else ""


def _strip_tool_suffix(value: str) -> str:
    lower = value.lower()
    if lower.endswith("_tool") and len(value) > 5:
        return value[:-5]
    if lower.endswith(".tool") and len(value) > 5:
        return value[:-5]
    return value


def _normalized_key(value: str) -> str:
    return _strip_tool_suffix(value).lower().replace("-", "_").replace(".", "_").replace("/", "_")

__all__ = ["resolve_dispatch_tool_name", "suggested_tool_name"]
