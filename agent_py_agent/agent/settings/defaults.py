
from __future__ import annotations

from functools import lru_cache
from typing import Any

DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS = 12_000
DEFAULT_COMMAND_ACCESS_MODE = "workspace-write"
# 模型单次回答的输出上限（含推理），所有模型统一 64K（用户 2026-09-27 要求）；随包 YAML 的 max_tokens 必须同值。
DEFAULT_MODEL_MAX_TOKENS = 65_536
# 输出上限最多占已知上下文窗口的几分之一：给输入留出至少四分之三窗口，避免输入加输出超窗被供应商拒绝。
MODEL_OUTPUT_WINDOW_DIVISOR = 4


# LLM: 输出上限夹取的唯一权威位置：窗口已明确（model_context_window_explicit 且窗口为正）时取
#   min(max_tokens, 窗口 // MODEL_OUTPUT_WINDOW_DIVISOR)，否则按配置原值。后端工厂用它决定实际发送的上限，
#   context_pressure 的输出预留读的也是后端上的这个值；不要在模型档案或别处再夹一次。
# 函数用途: 算出一次模型请求实际使用的输出 token 上限（不改配置本身）。
def effective_max_output_tokens(config: Any) -> int:
    configured = int(getattr(config, "max_tokens", DEFAULT_MODEL_MAX_TOKENS) or DEFAULT_MODEL_MAX_TOKENS)
    window = int(getattr(config, "model_context_window_tokens", 0) or 0)
    if not getattr(config, "model_context_window_explicit", False) or window <= 0:
        return configured
    return max(1, min(configured, window // MODEL_OUTPUT_WINDOW_DIVISOR))


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
    "MODEL_OUTPUT_WINDOW_DIVISOR",
    "effective_max_output_tokens",
    "default_agent_config",
    "default_config_bool",
    "default_config_float",
    "default_config_int",
    "default_config_value",
]
