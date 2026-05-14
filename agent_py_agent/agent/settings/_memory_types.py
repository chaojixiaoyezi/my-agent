"""Dataclasses for effective memory config and fallback warnings."""

# LLM: 这些 dataclass 是配置归一化结果的跨模块载体，字段名要保持兼容。
# 模块用途: 内存相关配置值和告警的数据模型。

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


# LLM: MemorySettings 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 内存配置归一化结果，保存启用项、预算和检索策略。
@dataclass(frozen=True)
class MemorySettings:
    """Effective memory config after validation and fallback normalization."""
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
    memory_compact_auto_allow_apply: bool = False
    memory_compact_context_window_tokens: int = 0
    memory_compact_auto_continue_max_depth: int = 1


# LLM: MemoryConfigWarning 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 内存配置告警，保存字段、原始值、回退值和原因。
@dataclass(frozen=True)
class MemoryConfigWarning:
    """Structured warning emitted when a memory config value falls back to default."""
    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str

    # LLM: MemoryConfigWarning.to_dict 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把MemoryConfigWarning转成可 JSON 持久化的字典。
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable warning payload for doctor/log output."""
        return asdict(self)
