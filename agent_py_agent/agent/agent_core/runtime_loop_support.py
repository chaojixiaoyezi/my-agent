# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from dataclasses import fields, replace

from ..memory_archive import build_auto_resume_context, has_resume_trigger
from ..memory_routing import RouteContextOptions, build_routed_memory_context
from .runtime_capabilities import resolve_runtime_capabilities
from .runtime_context_bundle import build_runtime_main_context_bundle
from .runtime_loop_models import (
    CompressionLoopResult,
    FinalizeParams,
    PreparedRuntimeContext,
    RunParams,
    RuntimeContextRequest,
    RuntimeLoopParams,
    RuntimeLoopResult,
    RuntimeToolLoopSeed,
)
from .runtime_services import CompressionContext, ToolLoopExecuteParams

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
    delivery_contract: dict | None = None,
    system_prompt_override: str | None = None,
    source: str | None = None,
    recovery_snapshot: bool | None = None,
    resume_context: bool | None = None,
    recovery_task_refs: list[str] | None = None,
    recovery_content_paths: list[str] | None = None,
    recovery_next_actions: list[str] | None = None,
    on_chunk: object = None,
    context_scope: str | None = None,
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
    prepared: PreparedRuntimeContext,
    params: RunParams,
) -> RuntimeLoopParams:
    return RuntimeLoopParams(
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
        delivery_contract=params.delivery_contract,
        system_prompt_override=params.system_prompt_override,
        on_chunk=params.on_chunk,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        source=params.source,
        context_scope=params.context_scope,
    )


# LLM: _finalize_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理finalize参数相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _finalize_params(
    user_prompt: str,
    prepared: PreparedRuntimeContext,
    loop_result: RuntimeLoopResult,
    run_params: RunParams,
) -> FinalizeParams:
    return FinalizeParams(
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
        main_context_bundle_path=prepared.main_context_bundle_path,
        main_context_bundle_markdown_path=prepared.main_context_bundle_markdown_path,
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


# LLM: _prepare_runtime_context prepares owner memory, routing, and resume sections from one bundle.
# 函数用途: 根据 RuntimeContextRequest 准备 memory、路由和恢复上下文；隔离上下文时不注入主代理长期记忆。
def _prepare_runtime_context(agent, request: RuntimeContextRequest):
    task_local = _is_task_local_context(request.context_scope)
    raw_memories = [] if task_local else agent.memory.search(request.user_prompt, agent.config.memory_top_k)
    memories = _memories_for_request(raw_memories, request, task_local=task_local)
    routed_context = _routed_memory_context_for_request(agent, request, task_local=task_local)
    resume_context_result, resume_context_section = _resume_context_for_request(
        agent, request, task_local=task_local,
    )
    base_runtime_injections = _base_runtime_injections(request, resume_context_section, routed_context)
    main_context_bundle = build_runtime_main_context_bundle(
        agent,
        request,
        memories=memories,
        runtime_injections=base_runtime_injections,
        routed_context=routed_context,
        resume_context_injected=bool(resume_context_result.injected),
        task_local=task_local,
    )
    runtime_injections = _runtime_injections_with_bundle(
        base_runtime_injections, len(request.inject or []), main_context_bundle,
    )
    return PreparedRuntimeContext(
        memories=memories,
        runtime_injections=runtime_injections,
        routed_context=routed_context,
        resume_context_result=resume_context_result,
        resume_context_section=resume_context_section,
        main_context_bundle_path=main_context_bundle.json_path if main_context_bundle else "",
        main_context_bundle_markdown_path=main_context_bundle.markdown_path if main_context_bundle else "",
    )


# LLM: _routed_memory_context_for_request isolates memory routing policy from run preparation.
# 函数用途: 根据上下文范围和配置生成路由记忆上下文；task-local 运行禁用主代理长期记忆路由。
def _routed_memory_context_for_request(agent, request: RuntimeContextRequest, *, task_local: bool):
    route_mode = str(getattr(agent.config, "memory_rule_routing_mode", "soft") or "soft")
    route_enabled = (
        not task_local and bool(getattr(agent.config, "memory_rule_routing_enabled", True)) and route_mode != "off"
    )
    route_auto_read_limit = int(getattr(agent.config, "memory_rule_auto_read_limit", 3))
    return build_routed_memory_context(
        agent.root,
        request.user_prompt,
        options=RouteContextOptions(
            enabled=route_enabled,
            mode=route_mode if route_mode != "off" else "soft",
            auto_read_limit=route_auto_read_limit,
            limit=max(route_auto_read_limit, 5),
        ),
    )


# LLM: _resume_context_for_request keeps auto-resume injection shape consistent for context bundles.
# 函数用途: 构造自动恢复上下文和对应 prompt 片段；task-local 运行不读主代理恢复上下文。
def _resume_context_for_request(agent, request: RuntimeContextRequest, *, task_local: bool):
    result = build_auto_resume_context(
        agent,
        request.user_prompt,
        enabled=False if task_local else request.resume_context,
    )
    section = f"### Auto Recovery Context\n{result.context_block}" if result.injected else ""
    return result, section


# LLM: _base_runtime_injections captures the pre-bundle injection count for diagnostics.
# 函数用途: 生成 context bundle 写入前的注入列表，用于计数和后续 token 估算。
def _base_runtime_injections(request: RuntimeContextRequest, resume_context_section: str, routed_context):
    return [
        *(request.inject or []),
        *([resume_context_section] if resume_context_section else []),
        *routed_context.injected_sections,
    ]


# LLM: _runtime_injections_with_bundle pins the order of user inject, bundle, resume, and routed sections.
# 函数用途: 按固定顺序生成最终运行时注入，避免调用方各自拼接导致 prompt 顺序漂移。
def _runtime_injections_with_bundle(
    base_runtime_injections: list,
    insert_at: int,
    main_context_bundle,
):
    if not main_context_bundle:
        return base_runtime_injections
    injections = list(base_runtime_injections)
    injections.insert(insert_at, main_context_bundle.prompt_section)
    return injections


# LLM: _execute_runtime_loop 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进运行时循环的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _execute_runtime_loop(agent, params: RuntimeLoopParams):
    tool_catalog_section, tool_recommendations_section = _resolve_tool_sections(
        agent, params.allowed_tools, params.granted_capabilities,
    )
    compression = _execute_runtime_compression(agent, params)
    loop_params = _tool_loop_execute_params(
        RuntimeToolLoopSeed(
            params=params,
            memories=compression.memories,
            tool_catalog_section=tool_catalog_section,
            tool_recommendations_section=tool_recommendations_section,
        )
    )
    final_prompt, final_response, tool_rounds = agent._get_services().tool_loop.execute(loop_params)
    return RuntimeLoopResult(
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
def _execute_runtime_compression(agent, params: RuntimeLoopParams) -> CompressionLoopResult:
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
    return CompressionLoopResult(memories=memories, snapshot_id=snapshot_id, snapshot_path=snapshot_path, applied=applied)


# LLM: _is_task_local_context prevents isolated turns from inheriting owner-level memory.
# 函数用途: 识别只允许隔离 refs 的运行模式，供 memory/routing/resume 注入共同使用。
def _is_task_local_context(value: object) -> bool:
    return str(value or "").strip().lower() in {"task_local", "control_plane"}


# LLM: _memories_for_request prevents one-shot execution from inheriting old task prompts.
# 函数用途: 对本轮可注入记忆做作用域过滤；CLI 一次性任务默认只保留规则/经验类事实，不把旧对话当当前任务。
def _memories_for_request(memories: list, request: RuntimeContextRequest, *, task_local: bool) -> list:
    if task_local:
        return []
    if _dialogue_memory_allowed(request):
        return memories
    return [memory for memory in memories if not _is_dialogue_memory(memory)]


# LLM: _dialogue_memory_allowed keeps explicit continuation stronger than standalone run isolation.
# 函数用途: 只有用户明确恢复/继续时，CLI run 才注入旧对话；chat/gateway 仍按会话长期上下文工作。
def _dialogue_memory_allowed(request: RuntimeContextRequest) -> bool:
    source = str(request.source or "").strip()
    if source != "cli_run":
        return True
    if request.resume_context is True:
        return True
    return has_resume_trigger(request.user_prompt)


def _is_dialogue_memory(memory: object) -> bool:
    return str(getattr(memory, "kind", "") or "").strip().lower() == "dialogue"


# LLM: _tool_loop_execute_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理工具循环execute参数相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _tool_loop_execute_params(seed: RuntimeToolLoopSeed) -> ToolLoopExecuteParams:
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
        delivery_contract=params.delivery_contract,
        system_prompt_override=params.system_prompt_override,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        run_scope=None,
        one_shot_tool_calls=one_shot_tool_calls,
        executed_tools=executed_tools,
        archive_tool_calls=archive_tool_calls,
        tool_rounds=tool_rounds,
        context_scope=params.context_scope,
    )
