# LLM: 部署默认直接取唯一 typed 守卫快照；原运行边界覆盖与 gate 仍独立，不增加授权。
# 模块用途: 将宿主工程限流策略投影到工具 gate，避免重复默认与原始配置字典。
from __future__ import annotations

from ..contracts.gates.tool_rate_limit import ToolRateLimitPolicy
from ..settings.runtime_guard_config import runtime_guard_policy


def tool_rate_limit_policy(boundary: dict[str, object] | None, policy: object = None) -> ToolRateLimitPolicy:
    default = default_tool_rate_limit_policy(policy)
    value = _boundary_mapping(boundary, "tool_rate_limit_policy")
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


# LLM: 只读传入的冻结快照；无快照时读唯一宿主策略，损坏源不能回落另一套数字。
# 函数用途: 将 typed 守卫字段一次投影为工具限流 gate 参数。
def default_tool_rate_limit_policy(policy: object = None) -> ToolRateLimitPolicy:
    effective = policy if policy is not None else runtime_guard_policy()
    return ToolRateLimitPolicy(
        max_calls=effective.tool_rate_max_calls,
        window_seconds=float(effective.tool_rate_window_seconds),
        failure_threshold=effective.tool_circuit_failure_threshold,
        backoff_schedule_seconds=effective.tool_circuit_backoff_seconds,
        max_records=effective.tool_rate_max_records,
    )


def _boundary_mapping(
    boundary: dict[str, object] | None,
    key: str,
) -> dict[str, object] | None:
    if not isinstance(boundary, dict):
        return None
    value = boundary.get(key)
    return value if isinstance(value, dict) else None


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
