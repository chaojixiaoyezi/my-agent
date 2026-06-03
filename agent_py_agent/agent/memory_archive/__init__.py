
from __future__ import annotations

"""public API for memory hook snapshots and raw archive storage.

新手说明:
这里是"压缩前快照"、"全量冷归档"和 runtime workspace adapter 的最小入口。
以后真实压缩流程要接入时，优先从这里导入数据结构和写入函数，不要把 JSONL 路径规则散落到别的模块里。
Daily ledger helpers are exported here so runtime memory callers do not invent path rules.
Artifact manifest helpers share the same public adapter surface.
Compact apply helpers are exported as non-destructive context/self-check writers.
Shared workspace helpers stay task-local and do not write main memory.
"""

from .agent_run_workspace import (
    AgentRunWorkspacePaths,
    EnsureAgentRunWorkspaceRequest,
    agent_run_workspace_paths,
    ensure_agent_run_workspace,
)
from .artifact.registry import (
    ArtifactManifestResult,
    SyncArtifactManifestsRequest,
    sync_artifact_manifests,
)
from .compact_action_guard import (
    CompactActionGuardOptions,
    CompactActionGuardRequest,
    build_compact_action_guard,
)
from .compact_apply import MemoryCompactApplyOptions, apply_memory_compact
from .compact_auto import MemoryCompactAutoCycleOptions, run_memory_compact_auto_cycle
from .compact_chain import (
    CompactChainResult,
    SyncAgentRunCompactChainRequest,
    default_compact_chain_result,
    sync_agent_run_compact_chain,
)
from .compact_continue_packet import CompactContinuePacketRequest, build_compact_continue_packet
from .compact_resume import MemoryCompactResumeOptions, build_memory_compact_resume
from .compact_suggest import MemoryCompactSuggestOptions, build_memory_compact_suggestion
from .control_plane import MemoryControlPlaneQueryOptions, query_memory_control_plane
from .daily_ledger import (
    AppendSubagentTaskEventRequest,
    DailyLedgerAppendResult,
    DailyLedgerWorkspaceRefs,
    append_subagent_task_event,
    daily_events_path_for,
)
from .models import CompressionSnapshot, RawMemoryEvent
from .resume_context import ResumeContextResult, build_auto_resume_context, has_resume_trigger
from .runtime import ArchiveRunTurnResult, archive_run_turn
from .schema import (
    RESERVED_FIELD_KEYS,
    RUNTIME_MEMORY_SCHEMA_VERSION,
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)
from .shared_workspace import (
    SharedWorkspaceResult,
    SyncSharedWorkspaceRequest,
    shared_workspace_paths,
    sync_shared_workspace,
)
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
from .task_workspace import (
    EnsureSubagentTaskWorkspaceRequest,
    TaskWorkspacePaths,
    ensure_subagent_task_workspace,
    task_workspace_path,
)
from .tokens import (
    TurnTokenUsage,
    append_session_token_usage,
    check_token_budget,
    estimate_tokens,
    token_ledger_dir,
)
from .tool_output_externalizer import ExternalizeToolOutputRequest, externalize_tool_output_record

__all__ = [
    "CompressionSnapshot",
    "ArchiveRunTurnResult",
    "AgentRunWorkspacePaths",
    "AppendSubagentTaskEventRequest",
    "ArtifactManifestResult",
    "CompactChainResult",
    "CompactContinuePacketRequest",
    "DailyLedgerAppendResult",
    "DailyLedgerWorkspaceRefs",
    "EnsureAgentRunWorkspaceRequest",
    "EnsureSubagentTaskWorkspaceRequest",
    "ExternalizeToolOutputRequest",
    "TurnTokenUsage",
    "CompressionHookResult",
    "CompactActionGuardOptions",
    "CompactActionGuardRequest",
    "MemoryArchiveError",
    "MemoryCompactApplyOptions",
    "MemoryCompactAutoCycleOptions",
    "MemoryCompactResumeOptions",
    "MemoryCompactSuggestOptions",
    "MemoryControlPlaneQueryOptions",
    "RawMemoryEvent",
    "RecoverySnapshotResult",
    "ResumeContextResult",
    "RESERVED_FIELD_KEYS",
    "RUNTIME_MEMORY_SCHEMA_VERSION",
    "RuntimeMemorySchemaOptions",
    "SharedWorkspaceResult",
    "SyncAgentRunCompactChainRequest",
    "SyncArtifactManifestsRequest",
    "SyncSharedWorkspaceRequest",
    "TaskWorkspacePaths",
    "archive_run_turn",
    "append_raw_event",
    "append_snapshot",
    "append_session_token_usage",
    "apply_memory_compact",
    "append_subagent_task_event",
    "build_auto_resume_context",
    "has_resume_trigger",
    "build_compact_continue_packet",
    "build_memory_compact_resume",
    "build_compact_action_guard",
    "build_memory_compact_suggestion",
    "run_memory_compact_auto_cycle",
    "check_token_budget",
    "clear_compression_hooks",
    "compression_snapshot_dir",
    "compression_snapshot_file_for",
    "daily_events_path_for",
    "default_compact_chain_result",
    "enforce_retention",
    "estimate_tokens",
    "externalize_tool_output_record",
    "agent_run_workspace_paths",
    "ensure_agent_run_workspace",
    "filter_raw_event_for_level",
    "filter_snapshot_for_level",
    "on_before_compression",
    "query_memory_control_plane",
    "raw_event_path_for",
    "register_compression_hook",
    "runtime_memory_reserved_fields",
    "runtime_memory_schema_payload",
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
