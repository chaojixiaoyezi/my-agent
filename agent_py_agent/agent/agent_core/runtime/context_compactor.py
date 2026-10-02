
from __future__ import annotations

from dataclasses import dataclass

from ...conversation.authority import conversation_transcript_is_authoritative
from ...memory_archive.compact_circuit_breaker import (
    DEFAULT_COMPACT_COOLDOWN_SECONDS,
    DEFAULT_COMPACT_FAILURE_THRESHOLD_COUNT,
)
from ..model.context_window import resolve_model_context_window_tokens

# LLM: These defaults define one provider-neutral runtime compact policy shared by root and child
# agents. The configured percentage has one authoritative location.
# 模块用途: 统一计算模型窗口、精确触发点、近期尾部预算和连续失败冷却参数。
DEFAULT_COMPACT_TRIGGER_PERCENT = 90
# 触发线绝对上限的默认值：与 AgentConfig/MemorySettings/随包 YAML 的默认一致；0 只在显式配置时表示不封顶。
DEFAULT_COMPACT_TRIGGER_MAX_TOKENS = 300_000
# compact 后目标上下文占用百分比，达到即停止压缩。
DEFAULT_COMPACT_RECOVERY_TARGET_PERCENT = 60
# compact 保留的最近对话轮数。
DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS = 4
# compact 尾部保留的 token 上限。
DEFAULT_COMPACT_RECENT_TAIL_MAX_TOKENS = 20_000
# compact 尾部占窗口的百分比，决定保留多少最近内容。
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

    # LLM: 只由结构化字段推出：上限严格小于「窗口 × 百分比」才算封顶；上限正好等于它时触发线与不封顶相同，
    #   finalization 仍按百分比判定。finalization 的 token 触发线、/context 展示和 recovery 的等比推导都据此切换；
    #   不开上限时恒为 False，原有行为不变。与 runtime_compact_policy 共用 _trigger_is_capped。
    # 函数用途: 判断这次的触发线是不是被绝对上限压下来的。
    @property
    def trigger_capped(self) -> bool:
        return _trigger_is_capped(self.context_window_tokens, self.trigger_percent, self.trigger_max_tokens)


# LLM: Resolve every durable compact limit from the model/config snapshot once per invocation.
# Transcript authority may permit a canonical Compact even when save=False only suppresses the
# legacy memory/final-response write path; auxiliary no-save calls still remain non-persistent.
# 触发线 = 窗口 × 百分比；memory_compact_auto_trigger_max_tokens 大于 0 时再与它取小（1M 窗口的长期后台线程靠它把压缩提前），
# 近期尾部与 recovery 目标都从封顶后的触发线推出：封顶时 recovery 按触发线 × recovery% ÷ 触发% 等比推导（_capped_recovery_target_tokens），
# 不封顶时仍是 compact_recovery_target_tokens 的原公式。所有自动压缩入口只读这里，不要在调用方另算一条触发线。
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
        DEFAULT_COMPACT_RECENT_TAIL_MAX_TOKENS,
        max(
            1,
            int(trigger_tokens * (DEFAULT_COMPACT_RECENT_TAIL_PERCENT / 100.0)),
        ),
    )
    if _trigger_is_capped(window, percent, trigger_max_tokens):
        recovery_target_tokens = _capped_recovery_target_tokens(trigger_tokens, percent, recovery_percent, recent_tail_tokens)
    else:
        recovery_target_tokens = compact_recovery_target_tokens(window, trigger_tokens, recent_tail_tokens, recovery_percent)
    return RuntimeCompactPolicy(
        context_window_tokens=window,
        trigger_percent=percent,
        trigger_tokens=trigger_tokens,
        recovery_target_percent=recovery_percent,
        recovery_target_tokens=recovery_target_tokens,
        allow_persistent_apply=(
            bool(save) or conversation_transcript_is_authoritative(task_attributes)
        ),
        recent_tail_max_turns=DEFAULT_COMPACT_RECENT_TAIL_MAX_TURNS,
        recent_tail_tokens=recent_tail_tokens,
        failure_threshold=DEFAULT_COMPACT_FAILURE_THRESHOLD_COUNT,
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


# LLM: 上限大于 0 且严格小于「窗口 × 百分比」才算封顶；上限等于或高于它时触发线与不封顶相同，各处按原百分比口径处理。
#   窗口未知（窗口 × 百分比为 0）时恒为 False。RuntimeCompactPolicy.trigger_capped 与 runtime_compact_policy 共用这一处。
# 函数用途: 判断绝对上限是否真的把触发线压低了。
def _trigger_is_capped(window: int, percent: int, trigger_max_tokens: int) -> bool:
    return 0 < trigger_max_tokens < compact_trigger_tokens(window, percent)


# LLM: 封顶时 recovery 的百分比部分按封顶后的触发线等比推导：触发线 × recovery% ÷ 触发%（上限 30 万、60%、90% 时是 20 万），
#   保持「恢复目标与触发线之比 = recovery% ÷ 触发%」；再与 compact_recovery_target_tokens 同口径，不超过「触发线 − 近期尾部」。
#   两个百分比都先经各自的规范化函数；先乘后整除，全程整数：浮点比值在部分百分比组合下会少 1（上限 10 万、80%、41% 应是 51,250）。
#   不封顶时不调用，原公式（窗口 × recovery%）不变。
# 函数用途: 算封顶触发线下压缩后的优选健康目标。
def _capped_recovery_target_tokens(trigger_tokens: int, percent: int, recovery_percent: int, recent_tail_tokens: int) -> int:
    proportional = int(trigger_tokens) * compact_recovery_target_percent(recovery_percent) // compact_trigger_percent(percent)
    tail_ceiling = int(trigger_tokens) - max(1, int(recent_tail_tokens or 0))
    return max(1, min(proportional, tail_ceiling))


# LLM: 与配置解析（_memory_coercion 的 int 字段，下限 0、无上限，非法回默认）同一口径：非整数、布尔、负数、缺失都回到
#   DEFAULT_COMPACT_TRIGGER_MAX_TOKENS；只有显式 0 表示不封顶。runtime_compact_policy 只在结果大于 0 时对触发线取小，
#   所以前台、后台和 finalization 自动压缩共用这一处。改默认值须同步 AgentConfig、MemorySettings、随包 YAML。
# 函数用途: 规范化自动压缩触发线的绝对 token 上限；0 表示不封顶，填错时回到安全默认值。
def compact_trigger_max_tokens(value: object) -> int:
    if isinstance(value, bool):
        return DEFAULT_COMPACT_TRIGGER_MAX_TOKENS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COMPACT_TRIGGER_MAX_TOKENS
    return parsed if parsed >= 0 else DEFAULT_COMPACT_TRIGGER_MAX_TOKENS


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
    "DEFAULT_COMPACT_RECENT_TAIL_MAX_TOKENS",
    "RuntimeCompactPolicy",
    "compact_recovery_target_percent",
    "compact_recovery_target_tokens",
    "compact_trigger_max_tokens",
    "compact_trigger_percent",
    "compact_trigger_tokens",
    "runtime_compact_policy",
]
