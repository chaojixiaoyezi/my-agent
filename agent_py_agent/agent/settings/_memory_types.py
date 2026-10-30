# LLM: 不可变 Memory 设置只保留 canonical 压缩比例，须与 YAML/AgentConfig 一致；管理员强制字段不能作为用户授权。
# 模块用途: 定义 Memory/Curator 生效配置和配置回退警告的数据结构。
"""Dataclasses for effective memory config and default warnings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


# LLM: 决策点位归原模型目录，不在 Memory schema 留副本；固定提炼模型只存档案编号，连接仍由原目录解析。
# 类用途: 保存校验后的Memory运行配置，闲置每拍限额须同步AgentConfig与YAML默认。
@dataclass(frozen=True)
class MemorySettings:
    """Effective memory config after validation and default normalization."""
    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_resume_auto_context_mode: str = "off"
    memory_compact_trigger_percent: int = 80
    force_compact_trigger_percent: bool = False
    memory_compact_idle_trigger_percent: int = 60
    memory_compact_idle_seconds: int = 7200
    memory_compact_idle_max_per_tick: int = 2
    memory_compact_auto_trigger_max_tokens: int = 300_000
    memory_compact_recovery_target_percent: int = 60
    memory_compact_summary_max_output_tokens: int = 16_384
    memory_compact_remote_enabled: bool = True
    memory_compact_reasoning_level: str = "low"
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


# LLM: 记忆配置诊断DTO；仅投影警告不修改配置或执行状态，联测配置coercion。
# 类用途: 承载记忆配置降级的结构化告警，供诊断和日志读取。
@dataclass(frozen=True)
class MemoryConfigWarning:
    """Structured warning emitted when a memory config value uses the default."""
    field_name: str
    raw_value: Any
    default_value: Any
    reason: str

    # LLM: 记忆配置诊断DTO；仅投影警告不修改配置或执行状态，联测配置coercion。
    # 函数用途: 将配置告警转成可序列化的公开字典。
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable warning payload for doctor/log output."""
        return asdict(self)
