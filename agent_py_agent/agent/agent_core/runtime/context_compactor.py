
from __future__ import annotations

from dataclasses import dataclass

from ..model.context_window import resolve_model_context_window_tokens

DEFAULT_COMPACT_TRIGGER_PERCENT = 90


@dataclass(frozen=True)
class RuntimeCompactPolicy:
    context_window_tokens: int
    trigger_percent: int
    trigger_tokens: int
    allow_persistent_apply: bool


def runtime_compact_policy(agent: object, *, save: bool = True) -> RuntimeCompactPolicy:
    window = resolve_model_context_window_tokens(agent)
    percent = compact_trigger_percent(getattr(getattr(agent, "config", None), "memory_compact_auto_trigger_percent", None))
    return RuntimeCompactPolicy(
        context_window_tokens=window,
        trigger_percent=percent,
        trigger_tokens=compact_trigger_tokens(window, percent),
        allow_persistent_apply=bool(save),
    )


def compact_trigger_percent(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COMPACT_TRIGGER_PERCENT
    if parsed <= 0:
        return 100
    if parsed < 50:
        return 50
    if parsed > 100:
        return 100
    return parsed


def compact_trigger_tokens(context_window_tokens: int, trigger_percent: int) -> int:
    if context_window_tokens <= 0:
        return 0
    return max(1, int(context_window_tokens * (compact_trigger_percent(trigger_percent) / 100.0)))


__all__ = [
    "DEFAULT_COMPACT_TRIGGER_PERCENT",
    "RuntimeCompactPolicy",
    "compact_trigger_percent",
    "compact_trigger_tokens",
    "runtime_compact_policy",
]
