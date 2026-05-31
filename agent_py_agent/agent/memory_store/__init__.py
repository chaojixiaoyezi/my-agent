# LLM: Memory store module; keep JSONL storage and indexing formats stable.
# 模块用途: 提供底层记忆 JSONL 存储、索引和读取能力。

from __future__ import annotations

"""public API for durable memory stores used by the agent runtime.

给人看的解释：
这里放'长期记忆怎么存'的实现。当前是 JSONL + LocalStore 索引，后面可以扩展
remote sync、compact、embedding index，但不要和普通配置或 prompt 混在一起。
"""

# LLM: daily memory exports expose the readable work journal beside raw JSONL.
from .daily import DailyMemoryEvent, append_daily_memory_event, daily_memory_path
from .jsonl import JsonlMemory, MemoryRecord

__all__ = [
    "DailyMemoryEvent",
    "JsonlMemory",
    "MemoryRecord",
    "append_daily_memory_event",
    "daily_memory_path",
]
