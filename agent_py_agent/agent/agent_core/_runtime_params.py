
from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from typing import Any

from ..memory_archive import (
    estimate_tokens,
)
from ..memory_archive.runtime.turn_archiver import ArchiveRunTurnParams, ArchiveTurnContext
from ..memory_archive.snapshots import (
    CompressionSnapshotInput,
    RecoverySnapshotInput,
)
from ..memory_archive.tokens import TurnTokenUsage, append_session_token_usage
from ..tools import ToolExecutionResult
from .models import AgentRunResult
from .parameters import _one_shot_tool_call_key


@dataclass(frozen=True)
class FinalizeContext:

    user_prompt: str
    final_prompt: str
    final_response: Any
    memories: list
    executed_tools: list
    archive_tool_calls: list
    routed_context: Any
    resume_context_result: Any
    runtime_injections: list
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    request_id: str
    run_id: str
    task_id: str
    source: str
    do_save: bool
    recovery_snapshot: Any
    recovery_task_refs: list | None
    recovery_content_paths: list | None
    recovery_next_actions: list | None
    tool_rounds: int = 0


@dataclass(frozen=True)
class ToolLoopExecuteParams:

    user_prompt: str
    memories: list
    runtime_injections: list
    prompt_files: list
    tool_catalog_section: str
    tool_recommendations_section: str
    tool_context: list
    effective_on_chunk: Any
    allowed_tools: Any
    granted_capabilities: Any
    write_boundary: Any
    task_attributes: dict | None
    one_shot_tool_calls: set
    executed_tools: list
    archive_tool_calls: list
    tool_rounds: int = 0


@dataclass(frozen=True)
class CompressionContext:

    user_prompt: str
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_section: str
    request_id: str
    run_id: str
    task_id: str
    source: str


@dataclass(frozen=True)
class ArchiveRunParams:

    do_save: bool
    user_prompt: str
    final_response: Any
    archive_tool_calls: list
    run_request_id: str
    run_id: str
    task_id: str
    source: str


@dataclass(frozen=True)
class WriteRecoverySnapshotParams:

    do_save: bool
    recovery_snapshot: Any
    user_prompt: str
    final_response: Any
    archive_tool_calls: list
    run_request_id: str
    run_id: str
    task_id: str
    source: str
    recovery_task_refs: list | None
    recovery_content_paths: list | None
    recovery_next_actions: list | None
    routed_context: Any


@dataclass(frozen=True)
class EstimateTokenParams:

    user_prompt: str
    runtime_injections: list
    memories: list
    final_response: Any
    archive_tool_calls: list
    run_request_id: str
    turn_id: str