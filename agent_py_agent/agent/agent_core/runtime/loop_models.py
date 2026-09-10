from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunParams:
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool | None = None
    allowed_tools: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str = ""
    # Host-generated identity for one concrete invocation of a stable run.
    # It is intentionally separate from request_id because scheduled work can
    # retry the same durable request in a fresh execution attempt.
    attempt_id: str = ""
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
    carried_active_turn_user_inputs: list[dict[str, object]] | None = None
    # Gateway injects one exact-turn transition callback. Runtime invokes it at
    # reservation and provider-submission edges; generic/local runs leave it empty.
    active_turn_transition_callback: object = None
    # Gateway-only typed callback used to publish the exact durable task selected
    # by this live request.  It is runtime state, never prompt text or persisted
    # task metadata, and survives ``dataclasses.replace`` continuations.
    conversation_task_binding_callback: object = None
    # The conversation layer supplies one immutable, already-bounded completed transcript seed.
    # Native protocol maps it to provider messages; text protocol renders it once.
    conversation_history_seed: object = None
    # CLI 自动续跑契约(2026-08-14 根因3 设计 v2): 首轮创建后贯穿所有续跑轮,
    # 保证同一 task/run/thread 链路(不每轮隐式生成新根)。
    # - continuation_seq: 0=首轮, 1..N=续跑轮(事件账本/终态分层用)
    # - continuation_root_task_id / root_run_id / root_thread_id: 首轮稳定 ID,
    #   续跑轮复用(不新建 task/run/thread 根)
    # - parent_attempt_id: 上一轮 attempt_id(树形链, 首轮为空)
    # - continuation_prompt: 本轮续跑提示(带 is_continuation 标记, 不伪装用户请求)
    continuation_seq: int = 0
    continuation_root_task_id: str = ""
    continuation_root_run_id: str = ""
    continuation_root_thread_id: str = ""
    continuation_root_request_id: str = ""
    continuation_parent_attempt_id: str = ""
    continuation_prompt: str = ""
    # 上一轮收口原因(缺口F, 双席复核 seq1835): 续跑消息结构化 metadata
    # 的一部分, 系统内部事件可追溯每轮续跑原因, 不依赖提示文本解析。
    continuation_reason: str = ""


@dataclass(frozen=True)
class RuntimeContextRequest:
    user_prompt: str
    inject: list[str] | None
    resume_context: bool | None
    context_scope: str = "default"
    allowed_tools: list[str] | None = None
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
    prompt_files: list | None = None
    write_boundary: dict | None = None
    task_attributes: dict | None = None
    delivery_contract: dict | None = None
    system_prompt_override: str | None = None
    on_chunk: object = None
    request_id: str = ""
    attempt_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    context_scope: str = "default"
    save: bool | None = None
    carried_archive_tool_calls: list[dict[str, object]] | None = None
    carried_active_turn_user_inputs: list[dict[str, object]] | None = None
    active_turn_transition_callback: object = None
    tool_runtime_snapshot: object = None
    tool_protocol_snapshot: object = None
    conversation_history_seed: object = None


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
    active_turn_user_inputs: list[dict[str, object]] | None = None
    tool_runtime_evidence: dict[str, object] | None = None
    canonical_native_messages: list[dict[str, object]] | None = None


@dataclass
class PreparedRuntimeContext:
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_result: Any
    resume_context_section: str
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""
    tool_runtime_snapshot: object = None
    tool_protocol_snapshot: object = None


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
    active_turn_user_inputs: list[dict[str, object]]
    tool_runtime_evidence: dict[str, object]
    canonical_native_messages: list[dict[str, object]]




@dataclass
class RuntimeToolLoopSeed:
    params: RuntimeLoopParams
    memories: list
    tool_catalog_section: str
    tool_recommendations_section: str
    tool_runtime_snapshot: object = None
    tool_protocol_snapshot: object = None
    effective_contract_snapshot: object = None
