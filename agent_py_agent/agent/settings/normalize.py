
# LLM: 保持这个兼容入口可用，新规则应优先落到 services 再由这里转发。
# 模块用途: AgentConfig 的旧归一化入口和子代理工作流配置校验。

from __future__ import annotations

from dataclasses import dataclass

from agent_py_agent.agent.settings.services._coercion import CoerceNumberParams, CoercionService
from agent_py_agent.agent.settings.services._normalize import AgentConfigNormalizer
from agent_py_agent.agent.settings.services._subagent import (
    SubagentWorkflowConfigService,
    SubagentWorkflowWarningService,
)

__all__ = [
    "_coerce_bool_config",
    "_coerce_choice_config",
    "_coerce_float_config",
    "_coerce_int_config",
    "normalize_agent_config",
    "normalize_subagent_workflow_config",
]


# ---------------------------------------------------------------------------
# Thin facade: delegate to service methods for backward compatibility
# ---------------------------------------------------------------------------


# LLM: _coerce_bool_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
def _coerce_bool_config(key: str, value: object, fallback: bool) -> tuple[bool, str | None]:
    """Coerce a raw config value to bool with a safe fallback."""
    return CoercionService.coerce_bool(key, value, fallback)


# LLM: _coerce_choice_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
def _coerce_choice_config(
    key: str, value: object, fallback: str, choices: tuple[str, ...]
) -> tuple[str, str | None]:
    """Coerce a raw config value to one of the allowed string choices."""
    return CoercionService.coerce_choice(key, value, fallback, choices)


# LLM: _coerce_float_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
def _coerce_float_config(
    key: str,
    value: object,
    fallback: float,
    *,
    params: CoerceNumberParams | None = None,
    min_val: float | None = None,
    max_val: float | None = None,
) -> tuple[float, str | None]:
    """Coerce a raw config value to float with optional range checks."""
    return CoercionService.coerce_float(key, value, fallback, params=params, min_val=min_val, max_val=max_val)


# LLM: _coerce_int_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
def _coerce_int_config(
    key: str,
    value: object,
    fallback: int,
    *,
    params: CoerceNumberParams | None = None,
    min_val: int | None = None,
    max_val: int | None = None,
) -> tuple[int, str | None]:
    """Coerce a raw config value to int with optional range checks."""
    return CoercionService.coerce_int(key, value, fallback, params=params, min_val=min_val, max_val=max_val)


# LLM: normalize_agent_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
def normalize_agent_config(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    return AgentConfigNormalizer.normalize(data)


# LLM: normalize_subagent_workflow_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
def normalize_subagent_workflow_config(config: object) -> list[dict[str, object]]:
    """validate and coerce subagent workflow config fields on an AgentConfig instance."""
    return SubagentWorkflowConfigService.normalize(config)


# LLM: SubagentWorkflowWarningParams 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentWorkflowWarningParams 参数包，把相关输入集中传给 配置系统 的服务函数。
@dataclass(frozen=True)
class SubagentWorkflowWarningParams:
    field_name: str
    raw_value: object
    fallback_value: object
    reason: str


# LLM: _add_subagent_workflow_warning 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 向结果或告警集合加入 add_subagent_workflow_warning，同时保留调用方依赖的顺序。
def _add_subagent_workflow_warning(
    warnings: list[dict[str, object]],
    *,
    field_name: str = "",
    raw_value: object = None,
    fallback_value: object = None,
    reason: str = "",
    params: SubagentWorkflowWarningParams | None = None,
) -> None:
    """Append a structured warning dict for a subagent workflow config field."""
    values = params or SubagentWorkflowWarningParams(field_name, raw_value, fallback_value, reason)
    SubagentWorkflowWarningService.add_warning(warnings, values)
