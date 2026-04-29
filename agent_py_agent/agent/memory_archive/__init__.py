from __future__ import annotations

"""LLM: public API for memory hook snapshots and raw archive storage.

给人看的解释：
这里是“压缩前快照”和“全量冷归档”的最小入口。
以后真实压缩流程要接入时，优先从这里导入数据结构和写入函数，不要把 JSONL 路径规则散落到别的模块里。
"""

from .models import CompressionSnapshot, RawMemoryEvent
from .runtime import ArchiveRunTurnResult, archive_run_turn
from .storage import (
    MemoryArchiveError,
    append_raw_event,
    append_snapshot,
    enforce_retention,
    raw_event_path_for,
    snapshot_path_for,
)
from .tokens import estimate_tokens

__all__ = [
    "CompressionSnapshot",
    "ArchiveRunTurnResult",
    "MemoryArchiveError",
    "RawMemoryEvent",
    "archive_run_turn",
    "append_raw_event",
    "append_snapshot",
    "enforce_retention",
    "estimate_tokens",
    "raw_event_path_for",
    "snapshot_path_for",
]
