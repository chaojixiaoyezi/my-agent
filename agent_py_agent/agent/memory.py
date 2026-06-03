
from __future__ import annotations

"""compatibility facade for memory storage moved to `agent.memory_store`.

真实记忆实现已经放到 `agent_py_agent.agent.memory_store`。
这个文件保留旧入口。
"""

from .memory_store import JsonlMemory, MemoryRecord

__all__ = ["JsonlMemory", "MemoryRecord"]
