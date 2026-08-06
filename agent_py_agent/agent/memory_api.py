from __future__ import annotations

"""CLI 与外围运行时可依赖的窄 Memory v2 公共接口。"""

# LLM: This module is an import facade only; every state transition remains owned by the one
# memory_store service, so exposing CLI-safe symbols cannot create a second authority or writer.
# 模块用途: 给 CLI 暴露稳定枚举和 Service 类型，阻止命令层穿透 Memory Store 内部包。

from .memory_store.candidate_models import (
    CANDIDATE_STATUSES,
    PROMOTION_TARGETS,
    PROPOSED_ACTIONS,
)
from .memory_store.daily import DAILY_ACTORS, DAILY_EVENT_TYPES, DailyMemoryStore
from .memory_store.lessons import LessonRepository
from .memory_store.lifecycle import request_memory_curator_for_session_best_effort
from .memory_store.migration import MemoryMigrationService
from .memory_store.retention import MemoryRetentionService

__all__ = [
    "CANDIDATE_STATUSES",
    "DAILY_ACTORS",
    "DAILY_EVENT_TYPES",
    "PROMOTION_TARGETS",
    "PROPOSED_ACTIONS",
    "DailyMemoryStore",
    "LessonRepository",
    "MemoryMigrationService",
    "MemoryRetentionService",
    "request_memory_curator_for_session_best_effort",
]
