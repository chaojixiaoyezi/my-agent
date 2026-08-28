
from __future__ import annotations

from dataclasses import dataclass

from ...conversation.authority import conversation_transcript_is_authoritative
from ..model.context_window import resolve_model_context_window_tokens

# LLM: These defaults define one provider-neutral runtime compact policy shared by root and child
# agents. The configured percentage has one authoritative location.
# 模块用途: 统一计算模型窗口、精确触发点、近期尾部预算和连续失败冷却参数。
DEFAULT_COMPACT_TRIGGER_PERCENT = 90
DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS = 4
DEFAULT_COMPACT_RECENT_TAIL_TOKEN_CAP = 20_000
DEFAULT_COMPACT_RECENT_TAIL_PERCENT = 10
DEFAULT_COMPACT_FAILURE_THRESHOLD = 3
DEFAULT_COMPACT_FAILURE_COOLDOWN_SECONDS = 300.0


# LLM: All compact callers consume this immutable resolved snapshot instead of recomputing limits.
# 类用途: 保存一次运行实际使用的窗口、触发点、近期尾部和熔断参数。
@dataclass(frozen=True)
class RuntimeCompactPolicy:
    context_window_tokens: int
    trigger_percent: int
    trigger_tokens: int
    allow_persistent_apply: bool
    recent_tail_max_turns: int
    recent_tail_tokens: int
    failure_threshold: int
    failure_cooldown_seconds: float


# LLM: Resolve every durable compact limit from the model/config snapshot once per invocation.
# Transcript authority may permit a canonical Compact even when save=False only suppresses the
# legacy memory/final-response write path; auxiliary no-save calls still remain non-persistent.
# 函数用途: 为主代理或子代理生成同一口径的压缩策略；会话权威回合即使由别处负责保存回复，也能提交真实压缩。
def runtime_compact_policy(
    agent: object,
    *,
    save: bool = True,
    context_scope: str = "default",
    task_attributes: object = None,
) -> RuntimeCompactPolicy:
    window = resolve_model_context_window_tokens(agent)
    percent = compact_trigger_percent(getattr(getattr(agent, "config", None), "memory_compact_auto_trigger_percent", None))
    return RuntimeCompactPolicy(
        context_window_tokens=window,
        trigger_percent=percent,
        trigger_tokens=compact_trigger_tokens(window, percent),
        allow_persistent_apply=(
            bool(save) or conversation_transcript_is_authoritative(task_attributes)
        ),
        recent_tail_max_turns=DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS,
        recent_tail_tokens=min(
            DEFAULT_COMPACT_RECENT_TAIL_TOKEN_CAP,
            max(
                1,
                int(
                    compact_trigger_tokens(window, percent)
                    * (DEFAULT_COMPACT_RECENT_TAIL_PERCENT / 100.0)
                ),
            ),
        ),
        failure_threshold=DEFAULT_COMPACT_FAILURE_THRESHOLD,
        failure_cooldown_seconds=DEFAULT_COMPACT_FAILURE_COOLDOWN_SECONDS,
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
    "DEFAULT_COMPACT_FAILURE_COOLDOWN_SECONDS",
    "DEFAULT_COMPACT_FAILURE_THRESHOLD",
    "DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS",
    "DEFAULT_COMPACT_RECENT_TAIL_PERCENT",
    "DEFAULT_COMPACT_RECENT_TAIL_TOKEN_CAP",
    "RuntimeCompactPolicy",
    "compact_trigger_percent",
    "compact_trigger_tokens",
    "runtime_compact_policy",
]
