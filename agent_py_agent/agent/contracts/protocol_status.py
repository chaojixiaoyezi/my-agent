"""Protocol-level status constants — canonical string values for tool results,
packet statuses, and runtime states.

These replace scattered inline string literals ("ready", "failed", "done", etc.)
with a single source of truth. The values stay as plain strings because they are
wire protocol values in JSON/dict payloads; new code should reference these
constants instead of raw string literals.

Natural-language status text (e.g. "完成了", "success", model-generated summaries)
must NOT be used for state-machine decisions; only these protocol values are valid.
"""

from __future__ import annotations

# ── Tool execution statuses ──────────────────────────────────────────────

TOOL_STATUS_DONE = "done"
TOOL_STATUS_FAILED = "failed"
TOOL_STATUS_RUNNING = "running"
TOOL_STATUS_PENDING = "pending"
TOOL_STATUS_TIMEOUT = "timeout"

TOOL_TERMINAL_STATUSES = frozenset({TOOL_STATUS_DONE, TOOL_STATUS_FAILED, TOOL_STATUS_TIMEOUT})


def is_tool_terminal(status: str) -> bool:
    """True when a tool execution has definitively finished (success or failure)."""
    return str(status or "").strip() in TOOL_TERMINAL_STATUSES


def is_tool_failed(status: str) -> bool:
    """True when tool execution resulted in a failure."""
    return str(status or "").strip() == TOOL_STATUS_FAILED


def is_tool_done(status: str) -> bool:
    """True when tool execution completed successfully."""
    return str(status or "").strip() == TOOL_STATUS_DONE


# ── Packet / continuation statuses ───────────────────────────────────────

PACKET_STATUS_READY = "ready"
PACKET_STATUS_NOT_READY = "not_ready"
PACKET_STATUS_DELIVERY_COMPLETE = "delivery_complete"

PACKET_READY_STATUSES = frozenset({PACKET_STATUS_READY})


def is_packet_ready(status: str) -> bool:
    """True when a continue packet is ready for resumption."""
    return str(status or "").strip() in PACKET_READY_STATUSES


# ── Compact auto-continue statuses ───────────────────────────────────────

COMPACT_STATUS_READY_AFTER_ACTION_GUARD = "ready_after_action_guard"
COMPACT_STATUS_ALLOWED_TO_CONTINUE = "allowed_to_continue"


# ── Background claim statuses ────────────────────────────────────────────

CLAIM_STATUS_RUNNING = "running"
CLAIM_STATUS_FINISHED = "finished"
CLAIM_STATUS_FAILED = "failed"
CLAIM_STATUS_CANCELLED = "cancelled"

CLAIM_FINISH_STATUSES = frozenset({CLAIM_STATUS_FINISHED, CLAIM_STATUS_FAILED, CLAIM_STATUS_CANCELLED})


# ── Wake signal statuses ─────────────────────────────────────────────────

WAKE_STATUS_PENDING = "pending"
WAKE_STATUS_HANDLED = "handled"


# ── Runtime guidance ─────────────────────────────────────────────────────

GUIDANCE_TARGET_TYPES = frozenset({"agent_run", "thread", "task", "case"})


__all__ = [
    "CLAIM_FINISH_STATUSES",
    "CLAIM_STATUS_CANCELLED",
    "CLAIM_STATUS_FAILED",
    "CLAIM_STATUS_FINISHED",
    "CLAIM_STATUS_RUNNING",
    "COMPACT_STATUS_ALLOWED_TO_CONTINUE",
    "COMPACT_STATUS_READY_AFTER_ACTION_GUARD",
    "GUIDANCE_TARGET_TYPES",
    "PACKET_READY_STATUSES",
    "PACKET_STATUS_DELIVERY_COMPLETE",
    "PACKET_STATUS_NOT_READY",
    "PACKET_STATUS_READY",
    "TOOL_STATUS_DONE",
    "TOOL_STATUS_FAILED",
    "TOOL_STATUS_PENDING",
    "TOOL_STATUS_RUNNING",
    "TOOL_STATUS_TIMEOUT",
    "TOOL_TERMINAL_STATUSES",
    "WAKE_STATUS_HANDLED",
    "WAKE_STATUS_PENDING",
    "is_packet_ready",
    "is_tool_done",
    "is_tool_failed",
    "is_tool_terminal",
]
