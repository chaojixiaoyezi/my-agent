
from __future__ import annotations

"""public API for durable memory stores used by the agent runtime.

这里放'长期记忆怎么存'的实现。当前是 JSONL + LocalStore 索引，后面可以扩展
remote sync、compact、embedding index，但不要和普通配置或 prompt 混在一起。
"""

from .daily import DailyMemoryEvent, append_daily_memory_event, daily_memory_path
from .jsonl import JsonlMemory, MemoryRecord

__all__ = [
    "DailyMemoryEvent",
    "JsonlMemory",
    "MemoryRecord",
    "append_daily_memory_event",
    "daily_memory_path",
]
