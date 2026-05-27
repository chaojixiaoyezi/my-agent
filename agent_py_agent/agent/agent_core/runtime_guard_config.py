# LLM: agent_core keeps this compatibility shim; the real reader lives in settings.
# 模块用途: 兼容旧导入路径，同时避免 tooling 反向导入 agent_core 造成循环依赖。

from __future__ import annotations

from ..settings.runtime_guard_config import (
    DEFAULT_RUNTIME_GUARD_CONFIG_PATH,
    runtime_guard_bool,
    runtime_guard_data,
    runtime_guard_float_tuple,
    runtime_guard_int,
)

__all__ = [
    "DEFAULT_RUNTIME_GUARD_CONFIG_PATH",
    "runtime_guard_bool",
    "runtime_guard_data",
    "runtime_guard_float_tuple",
    "runtime_guard_int",
]
