# LLM: DefaultConfigProvider is the only non-boundary place allowed to construct AgentConfig defaults.
# 模块用途: 集中提供配置 schema 默认值，避免底层模块各自裸建 AgentConfig 导致配置来源不可追踪。

from __future__ import annotations

from functools import lru_cache
from typing import Any


# LLM: default_agent_config returns a cached schema-default AgentConfig for fallback-only reads.
# 函数用途: 给无法取得运行时 config 的兼容路径提供默认值；正常运行链路应优先传入已加载 config。
@lru_cache(maxsize=1)
def default_agent_config() -> Any:
    from .config import AgentConfig

    return AgentConfig()


# LLM: default_config_value centralizes schema-default field lookup.
# 函数用途: 读取 AgentConfig 字段默认值；字段不存在时按 Python AttributeError 暴露代码问题。
def default_config_value(key: str) -> Any:
    return getattr(default_agent_config(), key)


def default_config_int(key: str, *, minimum: int | None = None) -> int:
    value = int(default_config_value(key))
    return max(minimum, value) if minimum is not None else value


def default_config_float(key: str, *, minimum: float | None = None) -> float:
    value = float(default_config_value(key))
    return max(minimum, value) if minimum is not None else value


def default_config_bool(key: str) -> bool:
    return bool(default_config_value(key))


__all__ = [
    "default_agent_config",
    "default_config_bool",
    "default_config_float",
    "default_config_int",
    "default_config_value",
]
