

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..action_protocol import RunScope


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
    recovery_task_refs: list | None
    recovery_content_paths: list | None
    recovery_next_actions: list | None
    tool_rounds: int = 0
    compact_auto_continue_depth: int = 0
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""


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
    request_id: str
    run_id: str
    task_id: str
    one_shot_tool_calls: set
    executed_tools: list
    archive_tool_calls: list
    tool_rounds: int = 0
    save: bool | None = None
    live_archive_state: dict[str, object] = field(default_factory=dict)
    system_prompt_override: str | None = None
    context_scope: str = "default"
    delivery_contract: dict | None = None
    run_scope: RunScope | None = None
    root_user_prompt: str = ""
    runtime_guard_policy: object | None = None


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
class EstimateTokenParams:

    user_prompt: str
    runtime_injections: list
    memories: list
    final_response: Any
    archive_tool_calls: list
    run_request_id: str
    turn_id: str
