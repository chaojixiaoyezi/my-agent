
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunParams:
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool | None = None
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    task_attributes: dict | None = None
    delivery_contract: dict | None = None
    system_prompt_override: str | None = None
    source: str = "run"
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None
    compact_auto_continue_depth: int = 0
    compact_auto_no_tool_continue_depth: int = 0
    context_scope: str = "default"
    root_user_prompt: str = ""
    carried_archive_tool_calls: list[dict[str, object]] | None = None


@dataclass(frozen=True)
class RuntimeContextRequest:
    user_prompt: str
    inject: list[str] | None
    resume_context: bool | None
    context_scope: str = "default"
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    save: bool | None = None
    task_attributes: dict | None = None


@dataclass
class RuntimeLoopParams:
    user_prompt: str
    root_user_prompt: str
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_section: str
    allowed_tools: list | None = None
    granted_capabilities: list | None = None
    prompt_files: list | None = None
    write_boundary: dict | None = None
    task_attributes: dict | None = None
    delivery_contract: dict | None = None
    system_prompt_override: str | None = None
    on_chunk: object = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    context_scope: str = "default"
    save: bool | None = None
    carried_archive_tool_calls: list[dict[str, object]] | None = None


@dataclass
class FinalizeParams:
    user_prompt: str
    final_prompt: str
    final_response: Any
    memories: list
    executed_tools: list[str]
    archive_tool_calls: list[dict[str, object]]
    routed_context: Any
    resume_context_result: Any
    runtime_injections: list
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    run_params: RunParams
    tool_rounds: int
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""


@dataclass
class PreparedRuntimeContext:
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_result: Any
    resume_context_section: str
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""


@dataclass
class RuntimeLoopResult:
    final_prompt: str
    final_response: Any
    tool_rounds: int
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    executed_tools: list[str]
    archive_tool_calls: list[dict[str, object]]


@dataclass
class CompressionLoopResult:
    memories: list
    snapshot_id: str
    snapshot_path: str
    applied: bool


@dataclass
class RuntimeToolLoopSeed:
    params: RuntimeLoopParams
    memories: list
    tool_catalog_section: str
    tool_recommendations_section: str
