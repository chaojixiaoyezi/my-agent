
from __future__ import annotations

from ...settings.runtime_guard_config import runtime_guard_bool, runtime_guard_int

DEFAULT_REPEAT_FAIL_THRESHOLD = 10


def repeat_fail_threshold(params: object) -> int:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        parsed = _int_value(attrs.get("repeat_fail_threshold"))
        if parsed >= 0:
            return parsed
    return runtime_guard_int(
        "repeat_fail_threshold",
        DEFAULT_REPEAT_FAIL_THRESHOLD,
        policy=getattr(params, "runtime_guard_policy", None),
    )


def terminal_block_enabled(params: object) -> bool:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and "terminal_block_enabled" in attrs:
        value = attrs.get("terminal_block_enabled")
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}
    else:
        return runtime_guard_bool(
            "terminal_block_enabled",
            False,
            policy=getattr(params, "runtime_guard_policy", None),
        )


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
