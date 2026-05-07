
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


def _coerce_bool_config(key: str, value: object, fallback: bool) -> tuple[bool, str | None]:
    """Coerce a raw config value to bool with a safe fallback."""
    return CoercionService.coerce_bool(key, value, fallback)


def _coerce_choice_config(
    key: str, value: object, fallback: str, choices: tuple[str, ...]
) -> tuple[str, str | None]:
    """Coerce a raw config value to one of the allowed string choices."""
    return CoercionService.coerce_choice(key, value, fallback, choices)


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


def normalize_agent_config(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    return AgentConfigNormalizer.normalize(data)


def normalize_subagent_workflow_config(config: object) -> list[dict[str, object]]:
    """LLM: validate and coerce subagent workflow config fields on an AgentConfig instance."""
    return SubagentWorkflowConfigService.normalize(config)


@dataclass(frozen=True)
class SubagentWorkflowWarningParams:
    # LLM: workflow warnings keep fallback fields grouped at the compatibility facade.
    field_name: str
    raw_value: object
    fallback_value: object
    reason: str


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
