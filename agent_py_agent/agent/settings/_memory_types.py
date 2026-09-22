"""Dataclasses for effective memory config and default warnings."""

# LLM: These immutable values are the normalized Memory runtime contract; YAML and AgentConfig defaults must stay aligned.
# 模块用途: 定义 Memory/Curator 生效配置和配置回退警告的数据结构。

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


# LLM: Curator/召回的决策默认归此类；可选时间和模型引用为 None 时继承通用策略，不改变记忆证据规则。
# 类用途: 保存校验后真正供 Memory 运行时使用的全部配置。
@dataclass(frozen=True)
class MemorySettings:
    """Effective memory config after validation and default normalization."""
    memory_decision_recall_mode: str = "off"
    memory_decision_recall_timeout_seconds: float | None = None
    memory_decision_recall_profile_id: str | None = None
    memory_decision_curator_mode: str = "off"
    memory_decision_curator_timeout_seconds: float | None = None
    memory_decision_curator_profile_id: str | None = None
    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_hook_archive_level: int = 3
    memory_hook_retention_days: int = 7
    memory_rule_routing_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_rule_receipt_enabled: bool = True
    memory_resume_auto_context_enabled: bool = False
    memory_resume_auto_context_mode: str = "trigger"
    memory_resume_auto_context_limit: int = 5
    memory_compact_auto_trigger_percent: int = 90
    memory_compact_recovery_target_percent: int = 60
    memory_curator_enabled: bool = True
    memory_curator_provider: str = "auto"
    memory_curator_model: str = ""
    memory_curator_interval_seconds: int = 10_800
    memory_curator_turn_threshold: int = 10
    memory_curator_batch_message_limit: int = 80
    memory_curator_max_input_chars: int = 40_000
    memory_curator_timeout_seconds: int = 90
    memory_curator_max_retries: int = 1
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
