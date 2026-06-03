
from __future__ import annotations

"""Helpers for converting AgentRunResult compact hints into subagent-local payloads."""


def subagent_session_compact_payload_from_result(result: object) -> dict[str, object]:
    if not bool(getattr(result, "memory_compact_suggested", False)):
        return {}
    return {
        "suggested": True,
        "status": str(getattr(result, "memory_compact_status", "") or ""),
        "auto_status": str(getattr(result, "memory_compact_auto_status", "") or ""),
        "ratio": float(getattr(result, "memory_compact_ratio", 0.0) or 0.0),
        "message": str(getattr(result, "memory_compact_message", "") or ""),
        "commands": list(getattr(result, "memory_compact_commands", []) or []),
        "token_budget": {
            "current_tokens": int(getattr(result, "cumulative_token_estimate", 0) or 0),
            "turn_tokens": int(getattr(result, "turn_token_estimate", 0) or 0),
            "prompt_tokens": int(getattr(result, "prompt_token_estimate", 0) or 0),
        },
        "continue_packet": dict(getattr(result, "memory_compact_auto_continue_packet", None) or {}),
    }


__all__ = ["subagent_session_compact_payload_from_result"]
