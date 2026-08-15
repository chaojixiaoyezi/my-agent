
from __future__ import annotations

from functools import lru_cache
from typing import Any

DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS = 12_000
DEFAULT_COMMAND_ACCESS_MODE = "workspace-write"
# 2026-08-15 3×3 对齐对照组(终端应用 的 Anthropic SDK 默认 ~32000):
# 16_314 是「每轮 1 工具」时代的保守值, 多工具并行(每轮 6-8 工具块)后偶发
# stop_reason=max_tokens 截断(cell2 实测死亡)。端点实测接受 40000。
DEFAULT_MODEL_MAX_TOKENS = 40_000


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
    "DEFAULT_MODEL_MAX_TOKENS",
    "DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS",
    "default_agent_config",
    "default_config_bool",
    "default_config_float",
    "default_config_int",
    "default_config_value",
]
