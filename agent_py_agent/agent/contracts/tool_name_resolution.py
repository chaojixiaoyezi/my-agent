
from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher

_SUGGESTION_THRESHOLD = 0.74


def suggested_tool_name(raw_name: object, available_tools: Iterable[str] | None) -> str:
    """Return a single close suggestion for diagnostics; callers must not execute it automatically."""
    raw = _normalized_key(str(raw_name or ""))
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


def _available_names(values: Iterable[str] | None) -> set[str]:
    return {str(item).strip() for item in values or [] if str(item).strip()}


def _normalized_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(".", "_").replace("/", "_")

__all__ = ["suggested_tool_name"]
