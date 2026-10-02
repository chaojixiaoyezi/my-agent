"""Dataclasses for effective memory config and default warnings."""

# LLM: 不可变 Memory 生效字段含召回前/后及 Curator 独立决策点；YAML 与 AgentConfig 默认必须一致，配置不构成写入权。
# 模块用途: 定义 Memory/Curator 生效配置和配置回退警告的数据结构。

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


# LLM: Curator 标签、关系与召回前/后各自默认关闭；点位的期限与模型引用只在决策设置覆盖层按点位设置，默认继承通用策略，
#   不改变记忆证据规则；固定提炼模型只存档案编号，服务商连接仍由原模型目录解析。
# 类用途: 保存校验后真正供 Memory 运行时使用的全部配置。
@dataclass(frozen=True)
class MemorySettings:
    """Effective memory config after validation and default normalization."""
    memory_decision_pre_recall_mode: str = "off"
    memory_decision_recall_mode: str = "off"
    memory_decision_curator_mode: str = "off"
    memory_decision_curator_relation_mode: str = "off"
    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_resume_auto_context_mode: str = "off"
    memory_compact_auto_trigger_percent: int = 90
    memory_compact_auto_trigger_max_tokens: int = 300_000
    memory_compact_recovery_target_percent: int = 60
    memory_curator_enabled: bool = True
    memory_curator_model_profile: str = ""
    memory_curator_interval_seconds: int = 10_800
    memory_curator_turn_threshold: int = 10
    memory_curator_max_input_chars: int = 40_000
    memory_curator_timeout_seconds: int = 90
    memory_curator_daily_finalize_hour: int = 23
    memory_curator_auto_promotion_policy: str = "conservative_v1"
    memory_lesson_min_occurrences: int = 2
    memory_hot_min_occurrences: int = 3


@dataclass(frozen=True)
class MemoryConfigWarning:
    """Structured warning emitted when a memory config value uses the default."""
    field_name: str
    raw_value: Any
    default_value: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable warning payload for doctor/log output."""
        return asdict(self)
