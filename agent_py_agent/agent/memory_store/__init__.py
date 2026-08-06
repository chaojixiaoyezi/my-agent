
from __future__ import annotations

"""public API for durable memory stores used by the agent runtime.

这里放'长期记忆怎么存'的实现。当前是 JSONL + LocalStore 索引，后面可以扩展
remote sync、compact、embedding index，但不要和普通配置或 prompt 混在一起。
"""

# LLM: This package facade exposes canonical Memory repositories only; it must not recreate legacy learning or gate aliases.
# 模块用途: 汇总长期事实、Daily、Recall、Migration 与 Retention 的稳定公开导入入口。

from .daily import DailyMemoryEvent, append_daily_memory_event, daily_memory_path
from .jsonl import JsonlMemory, MemoryRecord, MemorySubjectConflict
from .migration import (
    MEMORY_MIGRATION_SCHEMA_VERSION,
    MemoryMigrationError,
    MemoryMigrationFinding,
    MemoryMigrationReport,
    MemoryMigrationService,
)
from .recall import (
    MemoryRecallScope,
    hot_memory_records,
    long_term_record_matches_scope,
    routed_lesson_records,
)
from .retention import (
    RETENTION_SCHEMA_VERSION,
    MemoryRetentionPolicy,
    MemoryRetentionReport,
    MemoryRetentionService,
)

__all__ = [
    "DailyMemoryEvent",
    "JsonlMemory",
    "MEMORY_MIGRATION_SCHEMA_VERSION",
    "MemoryMigrationError",
    "MemoryMigrationFinding",
    "MemoryMigrationReport",
    "MemoryMigrationService",
    "MemoryRecord",
    "MemoryRecallScope",
    "MemoryRetentionPolicy",
    "MemoryRetentionReport",
    "MemoryRetentionService",
    "MemorySubjectConflict",
    "RETENTION_SCHEMA_VERSION",
    "append_daily_memory_event",
    "daily_memory_path",
    "hot_memory_records",
    "long_term_record_matches_scope",
    "routed_lesson_records",
]
