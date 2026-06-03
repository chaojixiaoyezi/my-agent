
from __future__ import annotations

"""compatibility facade for memory settings moved to `agent.settings.memory`.

memory 配置安全解析的真实实现已经放到 `agent_py_agent.agent.settings.memory`。
这个文件只保留旧导入入口，避免测试、旧代码或后续 AI 还在用 `agent.memory_settings` 时直接坏掉。
"""

from .settings.memory import (
    MemoryConfigWarning,
    MemorySettings,
    normalize_agent_memory_config,
    normalize_memory_settings,
)

__all__ = [
    "MemoryConfigWarning",
    "MemorySettings",
    "normalize_agent_memory_config",
    "normalize_memory_settings",
]
