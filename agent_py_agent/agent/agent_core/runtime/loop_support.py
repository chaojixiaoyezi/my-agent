
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace

from ...log_analysis.capabilities import has_security_tool_capability
from ...memory_archive import build_auto_resume_context, has_resume_trigger
from ...memory_routing import RouteContextOptions, build_routed_memory_context
from ...runtime_errors import runtime_error_report
from ...user_space.context_bundle import MainContextBundleRequest, build_main_context_bundle
from ...user_space.home_layout import runtime_route_root_and_index
from .._runtime_params import CompressionContext, ToolLoopExecuteParams
from .live_archive import write_runtime_fact_start_if_enabled
from .loop_models import (
    CompressionLoopResult,
    FinalizeParams,
    PreparedRuntimeContext,
    RunParams,
    RuntimeContextRequest,
    RuntimeLoopParams,
    RuntimeLoopResult,
    RuntimeToolLoopSeed,
)

_RUN_PARAM_FIELD_NAMES = tuple(field.name for field in fields(RunParams))
SECURITY_RUNTIME_CAPABILITY = "logs/security"


@dataclass(frozen=True)
class ToolSectionsRequest:
    agent: object
    user_prompt: str
    inject: object
    allowed_tools: list[str] | None
    granted_capabilities: list[str] | None


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


def _runtime_loop_params(
    user_prompt: str,
    prepared: PreparedRuntimeContext,
    params: RunParams,
) -> RuntimeLoopParams:
    return RuntimeLoopParams(
        user_prompt=user_prompt,
        root_user_prompt=params.root_user_prompt or user_prompt,
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
        save=params.save,
        carried_archive_tool_calls=params.carried_archive_tool_calls,
    )


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


def _resolve_tool_sections(request: ToolSectionsRequest):
    runtime_capabilities = resolve_runtime_capabilities(
        request.user_prompt,
        inject=request.inject,
        granted_capabilities=request.granted_capabilities,
    )
    if not request.agent.config.enable_tools:
        return "", ""
    tool_catalog = request.agent.tools.render_catalog_section(
        allowed_tools=request.allowed_tools,
        granted_capabilities=runtime_capabilities,
    )
    tool_recommendations = request.agent.tools.render_recommended_tools_section(
        request.user_prompt,
        allowed_tools=request.allowed_tools,
        granted_capabilities=runtime_capabilities,
    )
    return tool_catalog, tool_recommendations


def resolve_runtime_capabilities(
    user_prompt: str,
    *,
    inject: Iterable[str] | None = None,
    granted_capabilities: Iterable[str] | None = None,
) -> list[str]:
    capabilities = _normalize_capabilities(granted_capabilities)
    if has_security_tool_capability(capabilities):
        return capabilities

    _ = user_prompt
    if has_security_tool_capability(_normalize_capabilities(inject)):
        capabilities.append(SECURITY_RUNTIME_CAPABILITY)
    return capabilities


def _normalize_capabilities(capabilities: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in capabilities or []:
        item = str(raw).strip()
        key = item.lower()
        if item and key not in seen:
            normalized.append(item)
            seen.add(key)
    return normalized


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


def build_runtime_main_context_bundle(
    agent,
    request: RuntimeContextRequest,
    *,
    memories: list,
    runtime_injections: list,
    routed_context,
    resume_context_injected: bool,
    task_local: bool,
):
    if task_local:
        return None
    auto_save = bool(getattr(agent.config, "auto_save_memory", True))
    do_save = auto_save if request.save is None else bool(request.save)
    tool_specs, tool_spec_errors = _tool_specs_for_context(agent, request)
    return build_main_context_bundle(
        MainContextBundleRequest(
            root=agent.root,
            home_paths=getattr(agent, "home_paths", None),
            user_prompt=request.user_prompt,
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            source=request.source,
            context_scope=request.context_scope,
            save=do_save,
            memory_count=len(memories),
            runtime_injection_count=len(runtime_injections),
            routed_required_read_paths=tuple(getattr(routed_context, "required_read_paths", ()) or ()),
            routed_candidate_paths=tuple(getattr(routed_context, "candidate_paths", ()) or ()),
            resume_context_injected=resume_context_injected,
            task_attributes=request.task_attributes,
            workspace_roots=tuple(str(item) for item in getattr(agent, "workspace_roots", []) or ()),
            write_boundary=request.write_boundary,
            allowed_tools=tuple(request.allowed_tools or ()),
            granted_capabilities=tuple(request.granted_capabilities or ()),
            tool_specs=tuple(tool_specs),
            tool_spec_errors=tuple(tool_spec_errors),
        )
    )


def _tool_specs_for_context(agent, request: RuntimeContextRequest) -> tuple[list[object], list[dict[str, object]]]:
    try:
        return agent.tools.specs(
            allowed_tools=request.allowed_tools,
            granted_capabilities=request.granted_capabilities,
            include_orchestration=True,
        ), []
    except Exception as exc:
        return [], [runtime_error_report(exc, context="main_context_bundle.tool_specs")]


def _routed_memory_context_for_request(agent, request: RuntimeContextRequest, *, task_local: bool):
    route_mode = str(getattr(agent.config, "memory_rule_routing_mode", "soft") or "soft")
    route_enabled = (
        not task_local and bool(getattr(agent.config, "memory_rule_routing_enabled", True)) and route_mode != "off"
    )
    route_auto_read_limit = int(getattr(agent.config, "memory_rule_auto_read_limit", 3))
    route_root, route_index = runtime_route_root_and_index(agent)
    return build_routed_memory_context(
        route_root,
        request.user_prompt,
        options=RouteContextOptions(
            enabled=route_enabled,
            index_path=route_index,
            mode=route_mode if route_mode != "off" else "soft",
            auto_read_limit=route_auto_read_limit,
            limit=max(route_auto_read_limit, 5),
        ),
    )


def _resume_context_for_request(agent, request: RuntimeContextRequest, *, task_local: bool):
    result = build_auto_resume_context(
        agent,
        request.user_prompt,
        enabled=False if task_local else request.resume_context,
    )
    section = f"### Auto Recovery Context\n{result.context_block}" if result.injected else ""
    return result, section


def _base_runtime_injections(request: RuntimeContextRequest, resume_context_section: str, routed_context):
    return [
        *(request.inject or []),
        *([resume_context_section] if resume_context_section else []),
        *routed_context.injected_sections,
    ]


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


def _execute_runtime_loop(agent, params: RuntimeLoopParams):
    write_runtime_fact_start_if_enabled(agent, params)
    tool_catalog_section, tool_recommendations_section = _resolve_tool_sections(ToolSectionsRequest(
        agent=agent,
        user_prompt=params.user_prompt,
        inject=params.runtime_injections,
        allowed_tools=params.allowed_tools,
        granted_capabilities=params.granted_capabilities,
    ))
    compression = _execute_runtime_compression(agent, params)
    loop_params = _tool_loop_execute_params(
        agent,
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


def _execute_runtime_compression(agent, params: RuntimeLoopParams) -> CompressionLoopResult:
    compression_svc = agent._get_services().compression
    compression_ctx = CompressionContext(
        user_prompt=params.root_user_prompt or params.user_prompt,
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


def _is_task_local_context(value: object) -> bool:
    return str(value or "").strip().lower() in {"task_local", "control_plane"}


def _memories_for_request(memories: list, request: RuntimeContextRequest, *, task_local: bool) -> list:
    if task_local:
        return []
    if _dialogue_memory_allowed(request):
        return memories
    return [memory for memory in memories if not _is_dialogue_memory(memory)]


def _dialogue_memory_allowed(request: RuntimeContextRequest) -> bool:
    source = str(request.source or "").strip()
    if source != "cli_run":
        return True
    if request.resume_context is True:
        return True
    return has_resume_trigger(request.user_prompt)


def _is_dialogue_memory(memory: object) -> bool:
    return str(getattr(memory, "kind", "") or "").strip().lower() == "dialogue"


def _tool_loop_execute_params(agent, seed: RuntimeToolLoopSeed) -> ToolLoopExecuteParams:
    params = seed.params
    tool_context: list[str] = []
    tool_rounds = 0
    one_shot_tool_calls: set[str] = set()
    executed_tools: list[str] = []
    archive_tool_calls: list[dict[str, object]] = list(params.carried_archive_tool_calls or [])
    live_archive_state = _live_archive_state_from_carried_archive_tool_calls(archive_tool_calls)
    return ToolLoopExecuteParams(
        user_prompt=params.user_prompt,
        root_user_prompt=params.root_user_prompt or params.user_prompt,
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
        source=params.source,
        runtime_guard_policy=getattr(agent, "runtime_guard_policy", None),
        one_shot_tool_calls=one_shot_tool_calls,
        executed_tools=executed_tools,
        archive_tool_calls=archive_tool_calls,
        tool_rounds=tool_rounds,
        save=params.save,
        live_archive_state=live_archive_state,
        context_scope=params.context_scope,
    )


def _live_archive_state_from_carried_archive_tool_calls(records: list[dict[str, object]]) -> dict[str, object]:
    pending = _pending_deferred_tool_calls(records)
    if not pending:
        return {}
    return {"pending_deferred_tool_calls": pending}


def _pending_deferred_tool_calls(records: list[dict[str, object]]) -> list[dict[str, object]]:
    pending: dict[str, dict[str, object]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        params = _record_parameters(record)
        if not params:
            continue
        key = _tool_call_key(params)
        if not key:
            continue
        if _is_context_compact_deferred(record):
            pending[key] = params
            continue
        if bool(record.get("ok")) and key in pending:
            pending.pop(key, None)
    return list(pending.values())


def _record_parameters(record: dict[str, object]) -> dict[str, object]:
    params = record.get("parameters")
    if not isinstance(params, dict):
        return {}
    tool = str(params.get("tool") or record.get("tool") or "").strip()
    if not tool:
        return {}
    return {**params, "tool": tool}


def _is_context_compact_deferred(record: dict[str, object]) -> bool:
    return str(record.get("error_code") or "").strip() == "CONTEXT_COMPACT_DEFERRED"


def _tool_call_key(payload: dict[str, object]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return ""
