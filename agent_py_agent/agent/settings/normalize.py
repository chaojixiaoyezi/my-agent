

from __future__ import annotations

from agent_py_agent.agent.settings.services._coercion import CoerceNumberParams, CoercionService
from agent_py_agent.agent.settings.services._normalize import AgentConfigNormalizer

__all__ = [
    "_coerce_bool_config",
    "_coerce_choice_config",
    "_coerce_float_config",
    "_coerce_int_config",
    "normalize_agent_config",
]


# ---------------------------------------------------------------------------
# Normalize AgentConfig through settings services.
# ---------------------------------------------------------------------------


def _coerce_bool_config(key: str, value: object, default: bool) -> tuple[bool, str | None]:
    """Coerce a raw config value to bool with a safe default."""
    return CoercionService.coerce_bool(key, value, default)


def _coerce_choice_config(
    key: str, value: object, default: str, choices: tuple[str, ...]
) -> tuple[str, str | None]:
    """Coerce a raw config value to one of the allowed string choices."""
    return CoercionService.coerce_choice(key, value, default, choices)


def _coerce_float_config(
    key: str,
    value: object,
    default: float,
    *,
    params: CoerceNumberParams | None = None,
    min_val: float | None = None,
    max_val: float | None = None,
) -> tuple[float, str | None]:
    """Coerce a raw config value to float with optional range checks."""
    return CoercionService.coerce_float(key, value, default, params=params, min_val=min_val, max_val=max_val)


def _coerce_int_config(
    key: str,
    value: object,
    default: int,
    *,
    params: CoerceNumberParams | None = None,
    min_val: int | None = None,
    max_val: int | None = None,
) -> tuple[int, str | None]:
    """Coerce a raw config value to int with optional range checks."""
    return CoercionService.coerce_int(key, value, default, params=params, min_val=min_val, max_val=max_val)


def normalize_agent_config(data: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    return AgentConfigNormalizer.normalize(data)
