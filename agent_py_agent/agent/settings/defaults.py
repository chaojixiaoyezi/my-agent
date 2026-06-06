
from __future__ import annotations

from functools import lru_cache
from typing import Any

DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS = 12_000
DEFAULT_COMMAND_ACCESS_MODE = "workspace-write"


@lru_cache(maxsize=1)
def default_agent_config() -> Any:
    from .config import AgentConfig

    return AgentConfig()


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
    "DEFAULT_COMMAND_ACCESS_MODE",
    "DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS",
    "default_agent_config",
    "default_config_bool",
    "default_config_float",
    "default_config_int",
    "default_config_value",
]
