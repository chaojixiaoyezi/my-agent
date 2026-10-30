# LLM: 部署默认只取唯一冻结守卫；任务边界严格校验后仅收紧保护，不以零值关闭宿主硬门。
# 模块用途: 按真实限流、熔断及账本保留语义求交集，并投影到工具执行门。
from __future__ import annotations

import math

from ..contracts.gates.tool_rate_limit import ToolRateLimitPolicy
from ..settings.runtime_guard_config import runtime_guard_policy

_BOUNDARY_FIELDS = frozenset({
    "max_calls", "window_seconds", "failure_threshold", "backoff_schedule_seconds", "max_records",
})


# LLM: 零调用数及零失败阈值代表无限或关闭；窗口、退避和历史容量则必须沿保护方向取更大值。
# 函数用途: 将宿主策略与严格任务收紧输入合并，不修改任何输入对象。
def tool_rate_limit_policy(boundary: dict[str, object] | None, policy: object = None) -> ToolRateLimitPolicy:
    default = default_tool_rate_limit_policy(policy)
    value = _boundary_mapping(boundary, "tool_rate_limit_policy")
    if not value:
        return default
    if set(value) - _BOUNDARY_FIELDS:
        raise ValueError("tool_rate_limit_boundary_invalid")
    threshold = _upper_limit(
        default.failure_threshold,
        _int_value(value.get("failure_threshold", default.failure_threshold)),
    )
    return ToolRateLimitPolicy(
        max_calls=_upper_limit(default.max_calls, _int_value(value.get("max_calls", default.max_calls))),
        window_seconds=max(default.window_seconds, _float_value(value.get("window_seconds", default.window_seconds))),
        failure_threshold=threshold,
        backoff_schedule_seconds=_backoff_intersection(
            default, threshold,
            _float_tuple(value.get("backoff_schedule_seconds", default.backoff_schedule_seconds)),
        ),
        # 较小容量会先驱逐仍有效的宿主保护记录，不能把容量误当调用预算取最小值。
        max_records=max(
            default.max_records,
            _int_value(value.get("max_records", default.max_records), positive=True),
        ),
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


# LLM: 缺省节点才表示不额外收紧；显式损坏容器不能静默回落为另一份有效输入。
# 函数用途: 校验外层边界及限流策略节点的映射结构。
def _boundary_mapping(
    boundary: dict[str, object] | None,
    key: str,
) -> dict[str, object] | None:
    if boundary is None:
        return None
    if type(boundary) is not dict:
        raise ValueError("tool_rate_limit_boundary_invalid")
    if key not in boundary:
        return None
    value = boundary.get(key)
    if type(value) is not dict:
        raise ValueError("tool_rate_limit_boundary_invalid")
    return value


# LLM: 布尔、字符串、小数和负数不能通过强转冒充合法阈值；记录容量必须为正。
# 函数用途: 严格校验任务整数参数，错误只返回固定原因码。
def _int_value(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("tool_rate_limit_boundary_invalid")
    return value


# LLM: 等待和窗口只接有限非负数，拒绝真假值、非数字和浮点溢出，不能吞错改用默认。
# 函数用途: 严格校验并统一任务时间参数的数值类型。
def _float_value(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("tool_rate_limit_boundary_invalid")
    try:
        parsed = float(value)
    except OverflowError:
        raise ValueError("tool_rate_limit_boundary_invalid") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("tool_rate_limit_boundary_invalid")
    return parsed


# LLM: 退避序列必须非空且每项有效；跳过坏项会改变档位，空序列也不能关闭宿主退避。
# 函数用途: 校验完整任务退避序列并冻结为元组。
def _float_tuple(value: object) -> tuple[float, ...]:
    if type(value) not in (list, tuple) or not value:
        raise ValueError("tool_rate_limit_boundary_invalid")
    return tuple(_float_value(item) for item in value)


# LLM: 两个上限中零值代表无限，不参与正整数最小值比较。
# 函数用途: 合并调用预算或熔断阈值，保留宿主正上限。
def _upper_limit(host: int, requested: int) -> int:
    if host == 0:
        return requested
    return min(host, requested) if requested > 0 else host


# LLM: gate 索引是失败数减阈值；降低阈值后仍保留宿主绝对失败数退避，巨大阈值差不能展开为等长内存。
# 函数用途: 对齐宿主档位与任务下限取最大值；过长的宿主剩余阶段在末档保守保留最大等待。
def _backoff_intersection(
    host: ToolRateLimitPolicy, threshold: int, requested: tuple[float, ...],
) -> tuple[float, ...]:
    shift = max(0, host.failure_threshold - threshold) if threshold > 0 else 0
    schedule = host.backoff_schedule_seconds
    length = max(len(schedule) + min(shift, len(schedule)), len(requested))
    merged = [
        max(schedule[min(max(0, index - shift), len(schedule) - 1)],
            requested[min(index, len(requested) - 1)])
        for index in range(length)
    ]
    # 阈值差过大时不按差值分配内存，末档覆盖所有未展开的宿主阶段，宁可多等待也不缩短保护。
    if len(schedule) + shift > length:
        merged[-1] = max(merged[-1], max(schedule[max(0, length - 1 - shift):]))
    return tuple(merged)


__all__ = ["default_tool_rate_limit_policy", "tool_rate_limit_policy"]
