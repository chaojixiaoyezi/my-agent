from __future__ import annotations

from typing import Any


def current_context_token_estimate(payload: Any) -> int:
    """Return the token estimate humans expect for current context pressure."""
    for key in (
        "current_context_token_estimate",
        "prompt_token_estimate",
        "turn_token_estimate",
        "cumulative_token_estimate",
    ):
        value = _value(payload, key)
        if value > 0:
            return value
    return 0


def _value(payload: Any, key: str) -> int:
    raw = payload.get(key) if isinstance(payload, dict) else getattr(payload, key, 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["current_context_token_estimate"]
