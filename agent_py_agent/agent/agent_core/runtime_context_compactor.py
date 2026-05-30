# LLM: Runtime context compact policy is the single entry for compact thresholds and persistence boundaries.
# 模块用途: 统一主代理运行时 compact 的上下文窗口、触发百分比和保存边界，避免预检/收尾各自解析配置。

from __future__ import annotations

from dataclasses import dataclass

from .model_context_window import resolve_model_context_window_tokens

DEFAULT_COMPACT_TRIGGER_PERCENT = 90


# LLM: RuntimeCompactPolicy is a tiny immutable snapshot of the active compact policy.
# 类用途: 保存本次运行 compact 预算和写入边界；不读写文件，不执行 compact。
@dataclass(frozen=True)
class RuntimeCompactPolicy:
    context_window_tokens: int
    trigger_percent: int
    trigger_tokens: int
    allow_persistent_apply: bool


# LLM: runtime_compact_policy gives all compact callers the same threshold and save boundary.
# 函数用途: 从当前 agent 和 save 边界生成 compact 策略；用户只需要配置 trigger_percent。
def runtime_compact_policy(agent: object, *, save: bool = True) -> RuntimeCompactPolicy:
    window = resolve_model_context_window_tokens(agent)
    percent = compact_trigger_percent(getattr(getattr(agent, "config", None), "memory_compact_auto_trigger_percent", None))
    return RuntimeCompactPolicy(
        context_window_tokens=window,
        trigger_percent=percent,
        trigger_tokens=compact_trigger_tokens(window, percent),
        allow_persistent_apply=bool(save),
    )


# LLM: compact_trigger_percent keeps the only user-facing compact threshold simple and bounded.
# 函数用途: 解析自动 compact 百分比；0 表示 100，小于 50 抬到 50，坏值回默认 90。
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


# LLM: compact_trigger_tokens converts the percentage into a concrete model-window budget.
# 函数用途: 将上下文窗口和百分比转成 token 阈值；窗口未知时返回 0，让兜底触发仍可强制 compact。
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
