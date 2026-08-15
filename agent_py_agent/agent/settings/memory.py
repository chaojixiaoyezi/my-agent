
from __future__ import annotations

"""Normalize memory-related runtime config with safe defaults and warnings.

用户会手动改配置文件，所以这里是 memory 配置的当前入口；底层 coercion
只负责字段转换和 warning 生成。
"""

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from ._memory_coercion import (
    _MISSING,
    _build_memory_settings_dict,
    _coerce_bool,
    _coerce_choice,
    _coerce_int,
    _lookup,
    _warn,
)
from ._memory_types import MemoryConfigWarning, MemorySettings

__all__ = [
    "MemoryConfigWarning",
    "MemorySettings",
    "normalize_agent_memory_config",
    "normalize_memory_settings",
]


def normalize_memory_settings(
    values: Mapping[str, Any] | object | None = None,
) -> tuple[MemorySettings, list[MemoryConfigWarning]]:
    """Coerce raw memory config fields into effective settings plus default warnings."""
    source = values if values is not None else {}
    warnings: list[MemoryConfigWarning] = []
    defaults = MemorySettings()
    settings = MemorySettings(**_build_memory_settings_dict(source, defaults, warnings))
    return settings, warnings


def normalize_agent_memory_config(config: object) -> list[MemoryConfigWarning]:
    """Mutate an AgentConfig-like object so memory fields hold safe effective values."""
    settings, warnings = normalize_memory_settings(config)
    for field_name, value in asdict(settings).items():
        setattr(config, field_name, value)
    config.memory_config_warnings = [warning.to_dict() for warning in warnings]
    return warnings
