
from __future__ import annotations

"""defines agent runtime DTOs shared across the core mixins.

这个文件只放主代理运行结果这类小结构。
它不碰模型、不碰工具、不碰子代理，只给其他模块一个稳定的数据返回格式。
"""

from dataclasses import dataclass


# LLM: 运行结果保留内部 response，同时允许 Gateway 附加结构化 channel_delivery 用户投影；两者不能混用。
# 类用途: 描述一次主代理运行的完整内部结果及可选通道交付投影。
@dataclass
class AgentRunResult:

    prompt: str
    response: str
    backend: str
    used_memories: int
    tool_rounds: int = 0
    executed_tools: list[str] | None = None
    archive_tool_calls: list[dict[str, object]] | None = None
    memory_route_matches: int = 0
    memory_route_paths: list[str] | None = None
    archive_events: int = 0
    archive_token_estimate: int = 0
    prompt_token_estimate: int = 0
    runtime_injection_token_estimate: int = 0
    recovery_snapshot_id: str = ""
    recovery_snapshot_path: str = ""
    recovery_snapshot_error: str = ""
    recovery_snapshot_token_estimate: int = 0
    compression_snapshot_id: str = ""
    compression_snapshot_path: str = ""
    compression_applied: bool = False
    memory_resume_context_injected: bool = False
    memory_resume_context_query: str = ""
    memory_resume_context_matches: int = 0
    memory_resume_context_token_estimate: int = 0
    memory_resume_context_error: str = ""
    turn_token_estimate: int = 0
    cumulative_token_estimate: int = 0
    memory_compact_suggested: bool = False
    memory_compact_status: str = "ok"
    memory_compact_ratio: float = 0.0
    memory_compact_message: str = ""
    memory_compact_commands: list[str] | None = None
    memory_compact_trigger_reason: str = "normal_threshold"
    memory_compact_trigger_source: str = "token_budget"
    memory_compact_trigger_forced: bool = False
    memory_compact_auto_status: str = "skipped_below_threshold"
    memory_compact_auto_next_action: str = "continue_without_compact"
    memory_compact_auto_allowed_to_continue: bool = False
    memory_compact_auto_tool_execution: str = "none"
    memory_compact_auto_apply_id: str = ""
    memory_compact_auto_continue_ready: bool = False
    memory_compact_auto_continue_packet: dict | None = None
    memory_compact_auto_continued: bool = False
    memory_compact_auto_continued_from_apply_id: str = ""
    memory_compact_auto_continuation_depth: int = 0
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""
    runtime_status: str = "ok"
    runtime_reason: str = ""
    runtime_source: str = ""
    conversation_task_completed: bool = False
    delivery_artifacts: list[dict[str, object]] | None = None
    # Successful current-owner sends committed by send_message in this exact run.
    # Background/source-reply delivery consumes this typed evidence instead of
    # guessing from assistant prose or provider logs.
    message_tool_deliveries: list[dict[str, object]] | None = None
    # LLM: 当前请求副作用终态只能由 canonical operation ledger 生成，禁止解析用户可见正文。
    # 字段用途: 携带最终回复对应的结构化操作核验，供 CLI、Gateway 和通道安全投影。
    operation_verification: dict[str, object] | None = None
    conversation_persist_degraded: bool = False
    conversation_persist_error: str = ""
    channel_delivery: dict[str, object] | None = None
    # Internal continuation carrier for real user turns delivered while this
    # durable task was already running. Gateway public projections ignore it.
    active_turn_user_inputs: list[dict[str, object]] | None = None
