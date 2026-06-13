
from __future__ import annotations

from dataclasses import dataclass

from ..model.context_window import resolve_model_context_window_tokens

DEFAULT_COMPACT_TRIGGER_PERCENT = 70


@dataclass(frozen=True)
class RuntimeCompactPolicy:
    context_window_tokens: int
    trigger_percent: int
    trigger_tokens: int
    allow_persistent_apply: bool


def runtime_compact_policy(
    agent: object, *, save: bool = True, context_scope: str = "default"
) -> RuntimeCompactPolicy:
    window = resolve_model_context_window_tokens(agent)
    percent = compact_trigger_percent(getattr(getattr(agent, "config", None), "memory_compact_auto_trigger_percent", None))
    if context_scope == "task_local":
        # 子代理回合默认继承主代理触发点；capability_config 显式 >0 时才覆盖。
        override = _subagent_trigger_percent_override(agent)
        if override > 0:
            percent = compact_trigger_percent(override)
    return RuntimeCompactPolicy(
        context_window_tokens=window,
        trigger_percent=percent,
        trigger_tokens=compact_trigger_tokens(window, percent),
        allow_persistent_apply=bool(save),
    )


# LLM: 读 capability 配置统一走 capability_config_for_agent（唯一权威，含快照缓存）；
#   这里只做 subagent_compact_trigger_percent 的取值与容错。
# 函数用途: 取子代理 compact 触发百分比覆盖值；<=0 表示继承主代理配置。
def _subagent_trigger_percent_override(agent: object) -> int:
    from ....agent.capability.runtime_config_reload import capability_config_for_agent

    config = capability_config_for_agent(agent)
    try:
        return int(getattr(config, "subagent_compact_trigger_percent", 0) or 0)
    except (TypeError, ValueError):
        return 0


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
