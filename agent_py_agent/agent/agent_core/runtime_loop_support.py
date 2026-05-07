from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

from ..memory_archive import build_auto_resume_context
from ..memory_routing import RouteContextOptions, build_routed_memory_context
from .runtime_capabilities import resolve_runtime_capabilities
from .runtime_services import CompressionContext, ToolLoopExecuteParams


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
    source: str = "run"
    recovery_snapshot: bool | None = None
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None


@dataclass
class _RuntimeLoopParams:

    user_prompt: str
    memories: list
    runtime_injections: list
    allowed_tools: list | None = None
    granted_capabilities: list | None = None
    prompt_files: list | None = None
    write_boundary: dict | None = None
    task_attributes: dict | None = None
    on_chunk: object = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"


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


@dataclass
class _PreparedRuntimeContext:

    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_result: Any


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


@dataclass
class _CompressionLoopResult:
    memories: list
    snapshot_id: str
    snapshot_path: str
    applied: bool


@dataclass
class _RuntimeToolLoopSeed:
    params: _RuntimeLoopParams
    memories: list
    tool_catalog_section: str
    tool_recommendations_section: str


_RUN_PARAM_FIELD_NAMES = tuple(field.name for field in fields(RunParams))


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


def _runtime_loop_params(
    user_prompt: str,
    prepared: _PreparedRuntimeContext,
    params: RunParams,
) -> _RuntimeLoopParams:
    return _RuntimeLoopParams(
        user_prompt=user_prompt,
        memories=prepared.memories,
        runtime_injections=prepared.runtime_injections,
        allowed_tools=params.allowed_tools,
        granted_capabilities=params.granted_capabilities,
        prompt_files=params.prompt_files,
        write_boundary=params.write_boundary,
        task_attributes=params.task_attributes,
        on_chunk=params.on_chunk,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        source=params.source,
    )


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
    )


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


def _execute_runtime_compression(agent, params: _RuntimeLoopParams) -> _CompressionLoopResult:
    compression_svc = agent._get_services().compression
    compression_ctx = CompressionContext(
        user_prompt=params.user_prompt,
        memories=params.memories,
        runtime_injections=params.runtime_injections,
        routed_context=None,
        resume_context_section="",
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        source=params.source,
    )
    memories, snapshot_id, snapshot_path, applied = compression_svc.check_and_apply(compression_ctx)
    return _CompressionLoopResult(memories=memories, snapshot_id=snapshot_id, snapshot_path=snapshot_path, applied=applied)


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
        one_shot_tool_calls=one_shot_tool_calls,
        executed_tools=executed_tools,
        archive_tool_calls=archive_tool_calls,
        tool_rounds=tool_rounds,
    )
