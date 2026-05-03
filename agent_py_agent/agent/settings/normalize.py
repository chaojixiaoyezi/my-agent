"""LLM: config validation and coercion helpers – normalize raw YAML data into safe values.

给人看的解释：
这个文件负责"校验和修正"用户手写的 YAML 配置。
用户容易写错类型或超范围，这里的函数逐项检查，不合法的回退到安全默认值并给出警告。
主配置数据类 AgentConfig 和加载函数仍在 config.py。
"""

from __future__ import annotations

from agent_py_agent.agent.settings.services._coercion import CoercionService
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


def _coerce_bool_config(key: str, value: object, fallback: bool) -> tuple[bool, str | None]:
    """Coerce a raw config value to bool with a safe fallback."""
    return CoercionService.coerce_bool(key, value, fallback)


def _coerce_choice_config(
    key: str, value: object, fallback: str, choices: tuple[str, ...]
) -> tuple[str, str | None]:
    """Coerce a raw config value to one of the allowed string choices."""
    return CoercionService.coerce_choice(key, value, fallback, choices)


def _coerce_float_config(
    key: str, value: object, fallback: float, *, min_val: float | None = None, max_val: float | None = None
) -> tuple[float, str | None]:
    """Coerce a raw config value to float with optional range checks."""
    return CoercionService.coerce_float(key, value, fallback, min_val=min_val, max_val=max_val)


def _coerce_int_config(
    key: str, value: object, fallback: int, *, min_val: int | None = None, max_val: int | None = None
) -> tuple[int, str | None]:
    """Coerce a raw config value to int with optional range checks."""
    return CoercionService.coerce_int(key, value, fallback, min_val=min_val, max_val=max_val)


def normalize_agent_config(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    """LLM: validate and coerce all non-memory AgentConfig fields with safe fallbacks.

    Human version:
    用户手写 YAML 容易出错，这里逐项检查并回退到安全默认值。
    返回 (normalized_data, warnings)。
    """
    return AgentConfigNormalizer.normalize(data)


def normalize_subagent_workflow_config(config: object) -> list[dict[str, object]]:
    """LLM: validate and coerce subagent workflow config fields on an AgentConfig instance."""
    return SubagentWorkflowConfigService.normalize(config)


def _add_subagent_workflow_warning(
    warnings: list[dict[str, object]],
    field_name: str,
    raw_value: object,
    fallback_value: object,
    reason: str,
) -> None:
    """Append a structured warning dict for a subagent workflow config field."""
    SubagentWorkflowWarningService.add_warning(warnings, field_name, raw_value, fallback_value, reason)