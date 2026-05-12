# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

from ..memory_archive import build_auto_resume_context
from ..memory_routing import RouteContextOptions, build_routed_memory_context
from .runtime_capabilities import resolve_runtime_capabilities
from .runtime_services import CompressionContext, ToolLoopExecuteParams


# LLM: RunParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存run参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: _RuntimeLoopParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存运行时循环和压缩快照需要的上下文字段；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class _RuntimeLoopParams:
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


# LLM: _FinalizeParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存finalize参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class _FinalizeParams:
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


# LLM: _PreparedRuntimeContext 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存运行前准备出的 memory、路由和恢复上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class _PreparedRuntimeContext:
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_result: Any
    resume_context_section: str


# LLM: _RuntimeLoopResult 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存运行时循环结果字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class _RuntimeLoopResult:
    final_prompt: str
    final_response: Any
    tool_rounds: int
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    executed_tools: list[str]
    archive_tool_calls: list[dict[str, object]]


# LLM: _CompressionLoopResult 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存压缩循环结果字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class _CompressionLoopResult:
    memories: list
    snapshot_id: str
    snapshot_path: str
    applied: bool


# LLM: _RuntimeToolLoopSeed 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存运行时工具循环seed字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass
class _RuntimeToolLoopSeed:
    params: _RuntimeLoopParams
    memories: list
    tool_catalog_section: str
    tool_recommendations_section: str


_RUN_PARAM_FIELD_NAMES = tuple(field.name for field in fields(RunParams))


# LLM: run_params_from_values 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进来自参数values的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_params_from_values(
    params: RunParams | None = None,
    *,
    inject: list[str] | None = None,
    prompt_files: list[str] | None = None,
    save: bool | None = None,
    allowed_tools: list[str] | None = None,
    granted_capabilities: list[str] | None = None,
    write_boundary: dict[str, object] | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    task_attributes: dict | None = None,
    system_prompt_override: str | None = None,
    source: str | None = None,
    recovery_snapshot: bool | None = None,
    resume_context: bool | None = None,
    recovery_task_refs: list[str] | None = None,
    recovery_content_paths: list[str] | None = None,
    recovery_next_actions: list[str] | None = None,
    on_chunk: object = None,
) -> RunParams:
    if params is None:
        params = RunParams()
    elif not isinstance(params, RunParams):
        raise TypeError("run() requires params: RunParams keyword argument")

    values = {key: value for key, value in locals().items() if key != "params"}
    updates = {key: values[key] for key in _RUN_PARAM_FIELD_NAMES if values.get(key) is not None}
    if not updates:
        return params
    return replace(params, **updates)


# LLM: _runtime_loop_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进运行时循环参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runtime_loop_params(
    user_prompt: str,
    prepared: _PreparedRuntimeContext,
    params: RunParams,
) -> _RuntimeLoopParams:
    return _RuntimeLoopParams(
        user_prompt=user_prompt,
        memories=prepared.memories,
        runtime_injections=prepared.runtime_injections,
        routed_context=prepared.routed_context,
        resume_context_section=prepared.resume_context_section,
        allowed_tools=params.allowed_tools,
        granted_capabilities=params.granted_capabilities,
        prompt_files=params.prompt_files,
        write_boundary=params.write_boundary,
        task_attributes=params.task_attributes,
        system_prompt_override=params.system_prompt_override,
        on_chunk=params.on_chunk,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        source=params.source,
    )


# LLM: _finalize_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理finalize参数相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _finalize_params(
    user_prompt: str,
    prepared: _PreparedRuntimeContext,
    loop_result: _RuntimeLoopResult,
    run_params: RunParams,
) -> _FinalizeParams:
    return _FinalizeParams(
        user_prompt=user_prompt,
        final_prompt=loop_result.final_prompt,
        final_response=loop_result.final_response,
        memories=prepared.memories,
        executed_tools=loop_result.executed_tools,
        archive_tool_calls=loop_result.archive_tool_calls,
        routed_context=prepared.routed_context,
        resume_context_result=prepared.resume_context_result,
        runtime_injections=prepared.runtime_injections,
        compression_snapshot_id=loop_result.compression_snapshot_id,
        compression_snapshot_path=loop_result.compression_snapshot_path,
        compression_applied=loop_result.compression_applied,
        run_params=run_params,
        tool_rounds=loop_result.tool_rounds,
    )


# LLM: _resolve_tool_sections 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询工具sections需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _resolve_tool_sections(agent, allowed_tools, granted_capabilities):
    runtime_capabilities = resolve_runtime_capabilities(
        None, inject=None, granted_capabilities=granted_capabilities,
    )
    if not agent.config.enable_tools:
        return "", ""
    tool_catalog = agent.tools.render_catalog_section(
        allowed_tools=allowed_tools, granted_capabilities=runtime_capabilities,
    )
    tool_recommendations = agent.tools.render_recommended_tools_section(
        None, allowed_tools=allowed_tools, granted_capabilities=runtime_capabilities,
    )
    return tool_catalog, tool_recommendations


# LLM: _prepare_runtime_context 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理prepare运行时上下文相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _prepare_runtime_context(agent, user_prompt, inject, resume_context):
    memories = agent.memory.search(user_prompt, agent.config.memory_top_k)
    route_mode = str(getattr(agent.config, "memory_rule_routing_mode", "soft") or "soft")
    route_enabled = bool(getattr(agent.config, "memory_rule_routing_enabled", True)) and route_mode != "off"
    route_auto_read_limit = int(getattr(agent.config, "memory_rule_auto_read_limit", 3))
    routed_context = build_routed_memory_context(
        agent.root,
        user_prompt,
        options=RouteContextOptions(
            enabled=route_enabled,
            mode=route_mode if route_mode != "off" else "soft",
            auto_read_limit=route_auto_read_limit,
            limit=max(route_auto_read_limit, 5),
        ),
    )
    resume_context_result = build_auto_resume_context(agent, user_prompt, enabled=resume_context)
    resume_context_section = (
        f"### Auto Recovery Context\n{resume_context_result.context_block}"
        if resume_context_result.injected else ""
    )
    runtime_injections = [
        *(inject or []),
        *([resume_context_section] if resume_context_section else []),
        *routed_context.injected_sections,
    ]
    return _PreparedRuntimeContext(
        memories=memories,
        runtime_injections=runtime_injections,
        routed_context=routed_context,
        resume_context_result=resume_context_result,
        resume_context_section=resume_context_section,
    )


# LLM: _execute_runtime_loop 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进运行时循环的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _execute_runtime_loop(agent, params: _RuntimeLoopParams):
    tool_catalog_section, tool_recommendations_section = _resolve_tool_sections(
        agent, params.allowed_tools, params.granted_capabilities,
    )
    compression = _execute_runtime_compression(agent, params)
    loop_params = _tool_loop_execute_params(
        _RuntimeToolLoopSeed(
            params=params,
            memories=compression.memories,
            tool_catalog_section=tool_catalog_section,
            tool_recommendations_section=tool_recommendations_section,
        )
    )
    final_prompt, final_response, tool_rounds = agent._get_services().tool_loop.execute(loop_params)
    return _RuntimeLoopResult(
        final_prompt=final_prompt,
        final_response=final_response,
        tool_rounds=tool_rounds,
        compression_snapshot_id=compression.snapshot_id,
        compression_snapshot_path=compression.snapshot_path,
        compression_applied=compression.applied,
        executed_tools=loop_params.executed_tools,
        archive_tool_calls=loop_params.archive_tool_calls,
    )


# LLM: _execute_runtime_compression 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进运行时压缩的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _execute_runtime_compression(agent, params: _RuntimeLoopParams) -> _CompressionLoopResult:
    compression_svc = agent._get_services().compression
    compression_ctx = CompressionContext(
        user_prompt=params.user_prompt,
        memories=params.memories,
        runtime_injections=params.runtime_injections,
        routed_context=params.routed_context,
        resume_context_section=params.resume_context_section,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        source=params.source,
    )
    memories, snapshot_id, snapshot_path, applied = compression_svc.check_and_apply(compression_ctx)
    return _CompressionLoopResult(memories=memories, snapshot_id=snapshot_id, snapshot_path=snapshot_path, applied=applied)


# LLM: _tool_loop_execute_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理工具循环execute参数相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _tool_loop_execute_params(seed: _RuntimeToolLoopSeed) -> ToolLoopExecuteParams:
    params = seed.params
    tool_context: list[str] = []
    tool_rounds = 0
    one_shot_tool_calls: set[str] = set()
    executed_tools: list[str] = []
    archive_tool_calls: list[dict[str, object]] = []
    return ToolLoopExecuteParams(
        user_prompt=params.user_prompt,
        memories=seed.memories,
        runtime_injections=params.runtime_injections,
        prompt_files=params.prompt_files,
        tool_catalog_section=seed.tool_catalog_section,
        tool_recommendations_section=seed.tool_recommendations_section,
        tool_context=tool_context,
        effective_on_chunk=params.on_chunk,
        allowed_tools=params.allowed_tools,
        granted_capabilities=params.granted_capabilities,
        write_boundary=params.write_boundary,
        task_attributes=params.task_attributes,
        system_prompt_override=params.system_prompt_override,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        one_shot_tool_calls=one_shot_tool_calls,
        executed_tools=executed_tools,
        archive_tool_calls=archive_tool_calls,
        tool_rounds=tool_rounds,
    )
