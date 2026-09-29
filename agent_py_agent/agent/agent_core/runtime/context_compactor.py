
from __future__ import annotations

from dataclasses import dataclass

from ...conversation.authority import conversation_transcript_is_authoritative
from ...memory_archive.compact_circuit_breaker import (
    DEFAULT_COMPACT_COOLDOWN_SECONDS,
    DEFAULT_COMPACT_FAILURE_THRESHOLD,
)
from ..model.context_window import resolve_model_context_window_tokens

# LLM: These defaults define one provider-neutral runtime compact policy shared by root and child
# agents. The configured percentage has one authoritative location.
# 模块用途: 统一计算模型窗口、精确触发点、近期尾部预算和连续失败冷却参数。
DEFAULT_COMPACT_TRIGGER_PERCENT = 90
DEFAULT_COMPACT_RECOVERY_TARGET_PERCENT = 60
DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS = 4
DEFAULT_COMPACT_RECENT_TAIL_TOKEN_CAP = 20_000
DEFAULT_COMPACT_RECENT_TAIL_PERCENT = 10


# LLM: All compact callers consume this immutable resolved snapshot instead of recomputing limits.
# 类用途: 保存一次运行实际使用的窗口、触发点、近期尾部和熔断参数。
@dataclass(frozen=True)
class RuntimeCompactPolicy:
    context_window_tokens: int
    trigger_percent: int
    trigger_tokens: int
    recovery_target_percent: int
    recovery_target_tokens: int
    allow_persistent_apply: bool
    recent_tail_max_turns: int
    recent_tail_tokens: int
    failure_threshold: int
    failure_cooldown_seconds: float
    # memory_compact_auto_trigger_max_tokens 规范化后的绝对上限；0 表示不封顶。trigger_tokens 已按它取小。
    trigger_max_tokens: int = 0

    # LLM: 只由结构化字段推出：上限开启且触发线正好等于上限（窗口 × 百分比不低于上限）。finalization 的百分比判定与
    #   /context 展示据此改用 token 触发线；不开上限时恒为 False，原有行为不变。
    # 函数用途: 判断这次的触发线是不是被绝对上限压下来的。
    @property
    def trigger_capped(self) -> bool:
        return 0 < self.trigger_max_tokens == self.trigger_tokens


# LLM: Resolve every durable compact limit from the model/config snapshot once per invocation.
# Transcript authority may permit a canonical Compact even when save=False only suppresses the
# legacy memory/final-response write path; auxiliary no-save calls still remain non-persistent.
# 触发线 = 窗口 × 百分比；memory_compact_auto_trigger_max_tokens 大于 0 时再与它取小（1M 窗口的长期后台线程靠它把压缩提前），
# 近期尾部与 recovery 目标都从封顶后的触发线推出。所有自动压缩入口只读这里，不要在调用方另算一条触发线。
# 函数用途: 为主代理或子代理生成同一口径的压缩策略；会话权威回合即使由别处负责保存回复，也能提交真实压缩。
def runtime_compact_policy(
    agent: object,
    *,
    save: bool = True,
    context_scope: str = "default",
    task_attributes: object = None,
) -> RuntimeCompactPolicy:
    window = resolve_model_context_window_tokens(agent)
    config = getattr(agent, "config", None)
    percent = compact_trigger_percent(getattr(config, "memory_compact_auto_trigger_percent", None))
    recovery_percent = compact_recovery_target_percent(getattr(config, "memory_compact_recovery_target_percent", None))
    trigger_max_tokens = compact_trigger_max_tokens(getattr(config, "memory_compact_auto_trigger_max_tokens", None))
    trigger_tokens = _capped_trigger_tokens(window, percent, trigger_max_tokens)
    recent_tail_tokens = min(
        DEFAULT_COMPACT_RECENT_TAIL_TOKEN_CAP,
        max(
            1,
            int(trigger_tokens * (DEFAULT_COMPACT_RECENT_TAIL_PERCENT / 100.0)),
        ),
    )
    return RuntimeCompactPolicy(
        context_window_tokens=window,
        trigger_percent=percent,
        trigger_tokens=trigger_tokens,
        recovery_target_percent=recovery_percent,
        recovery_target_tokens=compact_recovery_target_tokens(
            window,
            trigger_tokens,
            recent_tail_tokens,
            recovery_percent,
        ),
        allow_persistent_apply=(
            bool(save) or conversation_transcript_is_authoritative(task_attributes)
        ),
        recent_tail_max_turns=DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS,
        recent_tail_tokens=recent_tail_tokens,
        failure_threshold=DEFAULT_COMPACT_FAILURE_THRESHOLD,
        failure_cooldown_seconds=DEFAULT_COMPACT_COOLDOWN_SECONDS,
        trigger_max_tokens=trigger_max_tokens,
    )


# LLM: 触发线 = 窗口 × 百分比；上限大于 0 时再与它取小。窗口未知（触发线为 0）时不因上限凭空造出触发线。
# 函数用途: 按窗口、百分比和绝对上限算出自动压缩触发线。
def _capped_trigger_tokens(window: int, percent: int, trigger_max_tokens: int) -> int:
    trigger_tokens = compact_trigger_tokens(window, percent)
    if trigger_max_tokens > 0 and trigger_tokens > 0:
        return min(trigger_tokens, trigger_max_tokens)
    return trigger_tokens


# LLM: 与配置解析（_memory_coercion 的 int 字段，下限 0、无上限）同一口径：非整数、布尔、负数都按 0（不封顶）；
#   runtime_compact_policy 只在结果大于 0 时对触发线取小，所以前台、后台和 finalization 自动压缩共用这一处。
# 函数用途: 规范化自动压缩触发线的绝对 token 上限；0 表示不封顶。
def compact_trigger_max_tokens(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


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


# LLM: Recovery is intentionally lower than the trigger and remains the compactor's preferred
# target. It is not a second validity threshold: 会话运行时 and 终端交互 keep a successful replacement
# once it is below the real trigger instead of discarding and reissuing the same summary request.
# 函数用途: 规范化压缩后的目标百分比；非法值回到 60%，过小或过大值限制在 25%--80%。
def compact_recovery_target_percent(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COMPACT_RECOVERY_TARGET_PERCENT
    if parsed < 25:
        return 25
    if parsed > 80:
        return 80
    return parsed


# LLM: Every live-tool and transcript Compact aims below both the configured recovery share and
# trigger-minus-tail ceiling. Callers may still commit a valid candidate below the trigger when
# fixed prompt/tool context makes this ideal target unreachable.
# 函数用途: 同时按模型窗口恢复比例和触发线尾部余量计算压缩后的统一优选健康目标。
def compact_recovery_target_tokens(
    context_window_tokens: int,
    trigger_tokens: int,
    recent_tail_tokens: int,
    recovery_target_percent: int,
) -> int:
    if context_window_tokens <= 0 or trigger_tokens <= 0:
        return 0
    percentage_target = max(
        1,
        int(
            int(context_window_tokens)
            * (compact_recovery_target_percent(recovery_target_percent) / 100.0)
        ),
    )
    tail_ceiling = max(
        1,
        int(trigger_tokens) - max(1, int(recent_tail_tokens or 0)),
    )
    return min(percentage_target, tail_ceiling)


__all__ = [
    "DEFAULT_COMPACT_TRIGGER_PERCENT",
    "DEFAULT_COMPACT_RECOVERY_TARGET_PERCENT",
    "DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS",
    "DEFAULT_COMPACT_RECENT_TAIL_PERCENT",
    "DEFAULT_COMPACT_RECENT_TAIL_TOKEN_CAP",
    "RuntimeCompactPolicy",
    "compact_recovery_target_percent",
    "compact_recovery_target_tokens",
    "compact_trigger_max_tokens",
    "compact_trigger_percent",
    "compact_trigger_tokens",
    "runtime_compact_policy",
]
