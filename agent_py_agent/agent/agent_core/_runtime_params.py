# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


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


# LLM: FinalizeContext 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存finalize上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: ToolLoopExecuteParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存工具循环 execute 参数、运行标识和可变记录容器；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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
    system_prompt_override: str | None = None


# LLM: CompressionContext 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存压缩上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: ArchiveRunParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存archiverun参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: WriteRecoverySnapshotParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存write恢复snapshot参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: EstimateTokenParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存estimate令牌参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class EstimateTokenParams:

    user_prompt: str
    runtime_injections: list
    memories: list
    final_response: Any
    archive_tool_calls: list
    run_request_id: str
    turn_id: str
