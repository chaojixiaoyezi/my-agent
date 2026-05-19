# LLM: Runtime loop model bundles keep orchestration code thin and bundle-first.
# 模块用途: 保存 SimpleAgent run/prepare/finalize/tool-loop 的参数包和结果包，不执行运行副作用。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# LLM: RunParams is the public run bundle; coordinate field changes with CLI, gateway, tests, and docs.
# 类用途: 集中保存 run 参数字段，让调用方按同一参数包传递上下文。
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
    system_prompt_override: str | None = None
    source: str = "run"
    recovery_snapshot: bool | None = None
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None
    compact_auto_continue_depth: int = 0
    compact_auto_continue_max_depth: int = 1
    context_scope: str = "default"
    background_intake: bool = False
    model_request_timeout_seconds: float | None = None


# LLM: RuntimeContextRequest bundles runtime context preparation inputs.
# 类用途: 替代 prepare 阶段散传参数，避免后续 memory/resume 入口继续增加位置参数。
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


# LLM: RuntimeLoopParams carries prepared prompt context into the tool loop.
# 类用途: 集中保存运行时循环和压缩快照需要的上下文字段。
@dataclass
class RuntimeLoopParams:
    user_prompt: str
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_section: str
    allowed_tools: list | None = None
    granted_capabilities: list | None = None
    prompt_files: list | None = None
    write_boundary: dict | None = None
    task_attributes: dict | None = None
    system_prompt_override: str | None = None
    on_chunk: object = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    context_scope: str = "default"
    background_intake: bool = False
    model_request_timeout_seconds: float | None = None


# LLM: FinalizeParams carries finalization facts after a run loop completes.
# 类用途: 汇总收尾阶段要写 archive、facts、resume snapshot 和最终响应的输入。
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


# LLM: PreparedRuntimeContext carries memory/routing/resume facts into prompt build.
# 类用途: 保存运行前准备出的 memory、路由和恢复上下文。
@dataclass
class PreparedRuntimeContext:
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_result: Any
    resume_context_section: str
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""


# LLM: RuntimeLoopResult records tool-loop results for finalization.
# 类用途: 保存运行时循环结果字段，让收尾阶段按同一 bundle 读取。
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


# LLM: CompressionLoopResult records memory compression output for one loop.
# 类用途: 保存压缩循环结果字段，供工具循环和最终归档继续使用。
@dataclass
class CompressionLoopResult:
    memories: list
    snapshot_id: str
    snapshot_path: str
    applied: bool


# LLM: RuntimeToolLoopSeed bundles tool catalog and compressed memory before tool execution.
# 类用途: 把工具循环起始状态打包，避免执行函数散传 catalog/recommendations/memories。
@dataclass
class RuntimeToolLoopSeed:
    params: RuntimeLoopParams
    memories: list
    tool_catalog_section: str
    tool_recommendations_section: str
