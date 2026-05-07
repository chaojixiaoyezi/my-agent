# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

from __future__ import annotations

"""compatibility facade for memory storage moved to `agent.memory_store`.

给人看的解释：
真实记忆实现已经放到 `agent_py_agent.agent.memory_store`。
这个文件保留旧入口。
"""

from .memory_store import JsonlMemory, MemoryRecord

__all__ = ["JsonlMemory", "MemoryRecord"]
