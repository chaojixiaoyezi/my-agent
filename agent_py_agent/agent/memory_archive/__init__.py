from __future__ import annotations

"""LLM: public API for memory hook snapshots and raw archive storage.

新手说明:
这里是"压缩前快照"和"全量冷归档"的最小入口。
以后真实压缩流程要接入时，优先从这里导入数据结构和写入函数，不要把 JSONL 路径规则散落到别的模块里。
"""

from .models import CompressionSnapshot, RawMemoryEvent
from .resume_context import ResumeContextResult, build_auto_resume_context
from .runtime import ArchiveRunTurnResult, archive_run_turn
from .snapshots import (
    CompressionHookResult,
    RecoverySnapshotResult,
    clear_compression_hooks,
    on_before_compression,
    register_compression_hook,
    write_compression_snapshot,
    write_recovery_snapshot,
)
from .storage import (
    MemoryArchiveError,
    append_raw_event,
    append_snapshot,
    compression_snapshot_dir,
    compression_snapshot_file_for,
    enforce_retention,
    filter_raw_event_for_level,
    filter_snapshot_for_level,
    raw_event_path_for,
    snapshot_path_for,
    write_compression_snapshot_file,
)
from .tokens import append_session_token_usage, check_token_budget, estimate_tokens, token_ledger_dir

__all__ = [
    "CompressionSnapshot",
    "ArchiveRunTurnResult",
    "CompressionHookResult",
    "MemoryArchiveError",
    "RawMemoryEvent",
    "RecoverySnapshotResult",
    "ResumeContextResult",
    "archive_run_turn",
    "append_raw_event",
    "append_snapshot",
    "append_session_token_usage",
    "build_auto_resume_context",
    "check_token_budget",
    "clear_compression_hooks",
    "compression_snapshot_dir",
    "compression_snapshot_file_for",
    "enforce_retention",
    "estimate_tokens",
    "filter_raw_event_for_level",
    "filter_snapshot_for_level",
    "on_before_compression",
    "raw_event_path_for",
    "register_compression_hook",
    "snapshot_path_for",
    "token_ledger_dir",
    "write_compression_snapshot",
    "write_compression_snapshot_file",
    "write_recovery_snapshot",
]
