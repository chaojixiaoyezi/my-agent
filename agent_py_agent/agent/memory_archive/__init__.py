from __future__ import annotations

"""LLM: public API for memory hook snapshots and raw archive storage.

新手说明:
这里是"压缩前快照"、"全量冷归档"和 runtime workspace adapter 的最小入口。
以后真实压缩流程要接入时，优先从这里导入数据结构和写入函数，不要把 JSONL 路径规则散落到别的模块里。
LLM: daily ledger helpers are exported here so runtime memory callers do not invent path rules.
LLM: artifact manifest helpers share the same public adapter surface.
LLM: compact chain helpers are exported without enabling destructive compact apply.
LLM: shared workspace helpers stay task-local and do not write main memory.
"""

# LLM: keep task/run workspace adapter exports centralized for callers.
from .agent_run_workspace import (
    AgentRunWorkspacePaths,
    agent_run_workspace_paths,
    ensure_agent_run_workspace,
)
from .artifact_registry import ArtifactManifestResult, sync_artifact_manifests
from .compact_chain import (
    CompactChainResult,
    default_compact_chain_result,
    sync_agent_run_compact_chain,
)
from .daily_ledger import (
    DailyLedgerAppendResult,
    DailyLedgerWorkspaceRefs,
    append_subagent_task_event,
    daily_events_path_for,
)
from .models import CompressionSnapshot, RawMemoryEvent
from .resume_context import ResumeContextResult, build_auto_resume_context
from .runtime import ArchiveRunTurnResult, archive_run_turn
from .shared_workspace import SharedWorkspaceResult, shared_workspace_paths, sync_shared_workspace
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
from .task_workspace import TaskWorkspacePaths, ensure_subagent_task_workspace, task_workspace_path
from .tokens import (
    TurnTokenUsage,
    append_session_token_usage,
    check_token_budget,
    estimate_tokens,
    token_ledger_dir,
)

__all__ = [
    "CompressionSnapshot",
    "ArchiveRunTurnResult",
    "AgentRunWorkspacePaths",
    "ArtifactManifestResult",
    "CompactChainResult",
    "DailyLedgerAppendResult",
    "DailyLedgerWorkspaceRefs",
    "TurnTokenUsage",
    "CompressionHookResult",
    "MemoryArchiveError",
    "RawMemoryEvent",
    "RecoverySnapshotResult",
    "ResumeContextResult",
    "SharedWorkspaceResult",
    "TaskWorkspacePaths",
    "archive_run_turn",
    "append_raw_event",
    "append_snapshot",
    "append_session_token_usage",
    "append_subagent_task_event",
    "build_auto_resume_context",
    "check_token_budget",
    "clear_compression_hooks",
    "compression_snapshot_dir",
    "compression_snapshot_file_for",
    "daily_events_path_for",
    "default_compact_chain_result",
    "enforce_retention",
    "estimate_tokens",
    "agent_run_workspace_paths",
    "ensure_agent_run_workspace",
    "filter_raw_event_for_level",
    "filter_snapshot_for_level",
    "on_before_compression",
    "raw_event_path_for",
    "register_compression_hook",
    "shared_workspace_paths",
    "snapshot_path_for",
    "ensure_subagent_task_workspace",
    "sync_artifact_manifests",
    "sync_agent_run_compact_chain",
    "sync_shared_workspace",
    "task_workspace_path",
    "token_ledger_dir",
    "write_compression_snapshot",
    "write_compression_snapshot_file",
    "write_recovery_snapshot",
]
