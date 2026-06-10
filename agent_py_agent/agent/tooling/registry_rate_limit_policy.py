from __future__ import annotations

from ..contracts.gates.tool_rate_limit import ToolRateLimitPolicy
from ..settings.runtime_guard_config import (
    runtime_guard_data,
    runtime_guard_float_tuple,
    runtime_guard_int,
)
from .registry_gate_policy import boundary_mapping


def tool_rate_limit_policy(boundary: dict[str, object] | None, policy: object = None) -> ToolRateLimitPolicy:
    default = default_tool_rate_limit_policy(policy)
    value = boundary_mapping(boundary, "tool_rate_limit_policy")
    if not value:
        return default
    return ToolRateLimitPolicy(
        max_calls=_int_value(value.get("max_calls"), default.max_calls),
        window_seconds=_float_value(value.get("window_seconds"), default.window_seconds),
        failure_threshold=_int_value(value.get("failure_threshold"), default.failure_threshold),
        backoff_schedule_seconds=_float_tuple(
            value.get("backoff_schedule_seconds"),
            default.backoff_schedule_seconds,
        ),
        max_records=_int_value(value.get("max_records"), default.max_records),
    )


def default_tool_rate_limit_policy(policy: object = None) -> ToolRateLimitPolicy:
    data = runtime_guard_data(policy=policy)
    return ToolRateLimitPolicy(
        max_calls=runtime_guard_int("tool_rate_max_calls", 60, policy=policy),
        window_seconds=_float_value(data.get("tool_rate_window_seconds"), 60.0),
        failure_threshold=runtime_guard_int("tool_circuit_failure_threshold", 3, policy=policy),
        backoff_schedule_seconds=runtime_guard_float_tuple(
            "tool_circuit_backoff_seconds",
            (1.0, 2.0, 4.0, 8.0, 16.0, 30.0),
            policy=policy,
        ),
        max_records=runtime_guard_int("tool_rate_max_records", 256, policy=policy),
    )


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _float_tuple(value: object, default: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, list | tuple):
        return default
    parsed: list[float] = []
    for item in value:
        try:
            parsed.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed) or default


__all__ = ["default_tool_rate_limit_policy", "tool_rate_limit_policy"]
