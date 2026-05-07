# LLM: 这里桥接旧字段和新内存配置对象，保留告警结构给 CLI 展示。
# 模块用途: 把原始 AgentConfig 中的 memory_* 字段归一化成 MemorySettings。

from __future__ import annotations

"""normalize memory-related runtime config with safe defaults and fallback warnings.

给人看的解释：
用户会手动改配置文件，所以这里专门负责把 memory 配置"洗干净"。
具体字段和底层 coercion 已拆到内部模块，本文件保持历史 public imports。
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


# LLM: normalize_memory_settings 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
def normalize_memory_settings(
    values: Mapping[str, Any] | object | None = None,
) -> tuple[MemorySettings, list[MemoryConfigWarning]]:
    """Coerce raw memory config fields into effective settings plus fallback warnings."""
    source = values if values is not None else {}
    warnings: list[MemoryConfigWarning] = []
    defaults = MemorySettings()
    settings = MemorySettings(**_build_memory_settings_dict(source, defaults, warnings))
    return settings, warnings


# LLM: normalize_agent_memory_config 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 配置系统 内部使用的稳定格式。
def normalize_agent_memory_config(config: object) -> list[MemoryConfigWarning]:
    """Mutate an AgentConfig-like object so memory fields hold safe effective values."""
    settings, warnings = normalize_memory_settings(config)
    for field_name, value in asdict(settings).items():
        setattr(config, field_name, value)
    config.memory_config_warnings = [warning.to_dict() for warning in warnings]
    return warnings
