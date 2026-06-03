
from __future__ import annotations

"""Shared pending-capability status helpers."""


def is_pending_capability_status(status: str) -> bool:
    normalized = str(status or "").upper().strip()
    if normalized in _EXPLICIT_PENDING_CAPABILITY_STATUSES:
        return True
    return _contains_waiting_and_capability_terms(normalized)


def _contains_waiting_and_capability_terms(normalized: str) -> bool:
    waiting_terms = ("PENDING", "NEED", "NEEDS", "WAIT", "WAITING", "REQUEST", "MISSING")
    capability_terms = ("CAPABILITY", "TOOL", "SKILL", "SHELL", "MCP")
    return any(term in normalized for term in waiting_terms) and any(
        term in normalized for term in capability_terms
    )


_EXPLICIT_PENDING_CAPABILITY_STATUSES = {
    "PENDING_CAPABILITY_REQUEST",
    "PENDING_CAPABILITY",
    "NEEDS_CAPABILITY",
    "NEED_CAPABILITY",
    "WAITING_FOR_CAPABILITY",
    "AWAITING_CAPABILITY",
    "CAPABILITY_REQUEST",
    "CAPABILITY_REQUESTED",
    "NEEDS_TOOL",
    "NEED_TOOL",
    "WAITING_FOR_TOOL",
    "NEEDS_SKILL",
    "NEEDS_SHELL",
    "NEEDS_MCP",
}
