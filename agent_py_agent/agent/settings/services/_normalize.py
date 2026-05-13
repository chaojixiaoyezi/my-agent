"""Domain-specific normalize services for config fields."""

# LLM: 这是新归一化管线的编排入口，新增字段服务要保持调用顺序可解释。
# 模块用途: 组合各配置字段服务，把 AgentConfig 原地归一化。

from __future__ import annotations

from ._normalize_core_fields import DaemonFieldsService, GatewayFieldsService, ModelFieldsService
from ._normalize_operational_fields import RuntimeBoolFieldsService
from ._normalize_runtime_fields import (
    AdapterFieldsService,
    SubagentAdvancedFieldsService,
    SubagentBasicFieldsService,
    ToolFieldsService,
    UserFieldsService,
)
from ._normalize_timeout_fields import TimeoutFieldsService

_NORMALIZE_SERVICES = (
    ModelFieldsService,
    GatewayFieldsService,
    DaemonFieldsService,
    ToolFieldsService,
    SubagentBasicFieldsService,
    AdapterFieldsService,
    UserFieldsService,
    RuntimeBoolFieldsService,
    TimeoutFieldsService,
    SubagentAdvancedFieldsService,
)


# LLM: AgentConfigNormalizer 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: AgentConfigNormalizer 数据模型，集中保存 配置系统 的结构化状态。
class AgentConfigNormalizer:
    """Service for normalizing all non-memory AgentConfig fields."""

    # LLM: AgentConfigNormalizer.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 AgentConfigNormalizer 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        """Validate and coerce all non-memory AgentConfig fields with safe fallbacks."""
        from ..config import AgentConfig

        warnings: list[str] = []
        out: dict[str, object] = dict(data)
        defaults = AgentConfig()

        for service in _NORMALIZE_SERVICES:
            out, service_warnings = service.normalize(out, defaults)
            warnings.extend(service_warnings)

        return out, warnings
