from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace

from ...conversation.active_turn_input import (
    active_turn_user_input_texts,
    merge_active_turn_user_inputs,
)
from ...conversation.authority import (
    CONVERSATION_BACKGROUND_EVENT_REASON_ATTR,
)
from ...memory_archive import build_auto_resume_context
from ...memory_routing import (
    RouteContextOptions,
    RoutedMemoryContext,
    build_routed_memory_context,
)
from ...memory_store import (
    MemoryRecallScope,
    hot_memory_records,
    long_term_record_matches_scope,
    routed_lesson_records,
)
from ...runtime_errors import runtime_error_report
from ...tooling.output_projection import project_tool_output_body
from ...user_space.context_bundle import MainContextBundleRequest, build_main_context_bundle
from ...user_space.home_layout import runtime_route_root_and_index
from .._runtime_params import CompressionContext, ToolLoopExecuteParams
from .._tool_loop_service import ToolLoopService
from ..parameters import _one_shot_tool_call_keys
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


@dataclass(frozen=True)
class ToolSectionsRequest:
    agent: object
    user_prompt: str
    allowed_tools: list[str] | None
    runtime_snapshot: object
    protocol_snapshot: object


# LLM: 只保留有真实运行消费者的字段；工具能力由 allowed_tools 与 runtime snapshot 共同决定。
# 函数用途: 将显式关键字覆盖合并进不可共享的 RunParams，供续跑和 compact 安全复用。
def run_params_from_values(
    params: RunParams | None = None,
    *,
    inject: list[str] | None = None,
    prompt_files: list[str] | None = None,
    save: bool | None = None,
    allowed_tools: list[str] | None = None,
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
        prompt_files=params.prompt_files,
        write_boundary=params.write_boundary,
        task_attributes=params.task_attributes,
        delivery_contract=params.delivery_contract,
        system_prompt_override=params.system_prompt_override,
        on_chunk=params.on_chunk,
        request_id=params.request_id,
        attempt_id=params.attempt_id,
        run_id=params.run_id,
        task_id=params.task_id,
        source=params.source,
        context_scope=params.context_scope,
        save=params.save,
        carried_archive_tool_calls=params.carried_archive_tool_calls,
        carried_active_turn_user_inputs=params.carried_active_turn_user_inputs,
        active_turn_transition_callback=params.active_turn_transition_callback,
        tool_runtime_snapshot=prepared.tool_runtime_snapshot,
        tool_protocol_snapshot=prepared.tool_protocol_snapshot,
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
        active_turn_user_inputs=loop_result.active_turn_user_inputs,
        tool_runtime_evidence=loop_result.tool_runtime_evidence,
    )


# LLM: 文本目录和推荐区必须消费 run 开始时的同一工具快照，不能各自重新探测或扩大权限。
# 函数用途: 用统一工具快照渲染本轮文本协议工具说明和相关工具建议。
def _resolve_tool_sections(request: ToolSectionsRequest):
    if not request.agent.config.enable_tools:
        return "", ""
    # The effective protocol is a runtime capability decision, not merely the
    # configured preference. A model that has downgraded to text must retain
    # the full text catalog; a native-capable model receives canonical schemas
    # through the provider's tools field and must not get the same schemas
    # duplicated in prompt prose.
    tool_protocol = str(getattr(request.protocol_snapshot, "source_protocol", "native") or "native")
    tool_catalog = request.agent.tools.render_catalog_section(
        allowed_tools=request.allowed_tools,
        tool_protocol=tool_protocol,
        runtime_snapshot=request.runtime_snapshot,
    )
    tool_recommendations = request.agent.tools.render_recommended_tools_section(
        request.user_prompt,
        allowed_tools=request.allowed_tools,
        tool_protocol=tool_protocol,
        runtime_snapshot=request.runtime_snapshot,
    )
    return tool_catalog, tool_recommendations


# LLM: Runtime recall uses only active long-term plus formal lesson/HOT under structured scope, then one memory-context envelope.
# 函数用途: 为一轮主代理请求准备 owner 隔离的 Memory、路由收据、恢复上下文与运行注入。
def _prepare_runtime_context(agent, request: RuntimeContextRequest):
    tool_runtime_snapshot, tool_protocol_snapshot = _tool_snapshots_for_run(
        agent,
        request,
    )
    task_local = _is_task_local_context(request.context_scope)
    memory_top_k = max(1, int(agent.config.memory_top_k or 1))
    routed_context = _routed_memory_context_for_request(agent, request, task_local=task_local)
    recall_scope = MemoryRecallScope.from_runtime(
        task_id=request.task_id,
        task_attributes=request.task_attributes,
    )
    # 真机缺口(2026-08-06):普通对话(无 task)的召回只含 global/personal,project 记忆(如
    # 用户喂入的小说知识库,scope=project:novel:xxx)不被召回 → 问小说"信息不足"。这里把
    # 记忆库中实际存在的 project scope 追加进召回范围(用户明确要求记住的项目知识可召回),
    # 有 task 时仍按 task 精确。纯结构化:只扫描 long_term 的 attributes.scope_type/key。
    # gateway 的 request_id 会被当 task_id,故不能以 task_id 判"普通对话"。始终追加记忆库中
    # 实际存在的 project scope(用户明确要求记住的项目知识可召回),与 task 的 project:task 并存。
    try:
        project_pairs: list[tuple[str, str]] = []
        for record in agent.memory.all():
            attrs = record.attributes if isinstance(record.attributes, dict) else {}
            st = str(attrs.get("scope_type") or "").strip().lower()
            sk = str(attrs.get("scope_key") or "").strip()
            if st == "project" and sk:
                project_pairs.append(("project", sk))
        if project_pairs:
            recall_scope = MemoryRecallScope(tuple(dict.fromkeys([*recall_scope.keys, *project_pairs])))
    except Exception:
        pass

    long_term_memories = (
        []
        if task_local
        else agent.memory.search_scoped(
            request.user_prompt,
            memory_top_k,
            lambda record: long_term_record_matches_scope(record, recall_scope),
        )
    )

    memories = _formal_memories_for_request(
        agent,
        request,
        routed_context,
        recall_scope=recall_scope,
        long_term_memories=long_term_memories,
        task_local=task_local,
    )

    resume_context_result, resume_context_section = _resume_context_for_request(
        agent,
        request,
        task_local=task_local,
    )
    base_runtime_injections = _base_runtime_injections(
        request, resume_context_section, routed_context
    )
    main_context_bundle = build_runtime_main_context_bundle(
        agent,
        request,
        memories=memories,
        runtime_injections=base_runtime_injections,
        routed_context=routed_context,
        resume_context_injected=bool(resume_context_result.injected),
        task_local=task_local,
        tool_runtime_snapshot=tool_runtime_snapshot,
    )
    runtime_injections = _runtime_injections_with_bundle(
        base_runtime_injections,
        len(request.inject or []),
        main_context_bundle,
    )
    return PreparedRuntimeContext(
        memories=memories,
        runtime_injections=runtime_injections,
        routed_context=routed_context,
        resume_context_result=resume_context_result,
        resume_context_section=resume_context_section,
        main_context_bundle_path=main_context_bundle.json_path if main_context_bundle else "",
        main_context_bundle_markdown_path=main_context_bundle.markdown_path
        if main_context_bundle
        else "",
        tool_runtime_snapshot=tool_runtime_snapshot,
        tool_protocol_snapshot=tool_protocol_snapshot,
    )


def _tool_snapshots_for_run(
    agent: object,
    request: RuntimeContextRequest,
) -> tuple[object, object]:
    """Freeze protocol and tools once, before any catalog/context projection."""

    from ...tooling.models import ToolRuntimeSnapshot
    from ..native_tool_protocol import select_tool_protocol

    protocol_snapshot = select_tool_protocol(agent, run_id=request.run_id)
    if agent.config.enable_tools:
        runtime_snapshot = agent.tools.runtime_snapshot(
            allowed_tools=request.allowed_tools,
            run_id=request.run_id,
        )
    else:
        runtime_snapshot = ToolRuntimeSnapshot(
            run_id=request.run_id,
            runtimes=(),
            available_tool_names=frozenset(),
            unavailable_tools=(),
            allowed_tools=(
                frozenset(request.allowed_tools) if request.allowed_tools is not None else None
            ),
            owner_type=str(getattr(agent.tools, "owner_type", "main_agent") or "main_agent"),
        )
    return runtime_snapshot, protocol_snapshot


def build_runtime_main_context_bundle(
    agent,
    request: RuntimeContextRequest,
    *,
    memories: list,
    runtime_injections: list,
    routed_context,
    resume_context_injected: bool,
    task_local: bool,
    tool_runtime_snapshot: object = None,
):
    if task_local:
        return None
    workspace_root = getattr(agent, "effective_workspace_root", agent.root)
    workspace_roots = getattr(
        agent,
        "effective_workspace_roots",
        getattr(agent, "workspace_roots", [workspace_root]),
    )
    auto_save = bool(getattr(agent.config, "auto_save_memory", True))
    do_save = auto_save if request.save is None else bool(request.save)
    runtime_snapshot, tool_runtime_errors = _tool_runtime_for_context_bundle(
        agent,
        request,
        tool_runtime_snapshot,
    )
    return build_main_context_bundle(
        MainContextBundleRequest(
            root=workspace_root,
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
            routed_required_read_paths=tuple(
                getattr(routed_context, "required_read_paths", ()) or ()
            ),
            routed_candidate_paths=tuple(getattr(routed_context, "candidate_paths", ()) or ()),
            resume_context_injected=resume_context_injected,
            task_attributes=request.task_attributes,
            workspace_roots=tuple(str(item) for item in workspace_roots or ()),
            write_boundary=request.write_boundary,
            tool_runtime_snapshot=runtime_snapshot,
            tool_runtime_errors=tuple(tool_runtime_errors),
        )
    )


def _tool_runtime_for_context_bundle(
    agent: object,
    request: RuntimeContextRequest,
    snapshot: object,
) -> tuple[object, list[dict[str, object]]]:
    from ...tooling.models import ToolRuntimeSnapshot

    if isinstance(snapshot, ToolRuntimeSnapshot):
        return snapshot, []
    try:
        return agent.tools.runtime_snapshot(
            allowed_tools=request.allowed_tools,
            run_id=request.run_id,
        ), []
    except Exception as exc:
        empty = ToolRuntimeSnapshot(
            run_id=request.run_id,
            runtimes=(),
            available_tool_names=frozenset(),
            unavailable_tools=(),
            allowed_tools=(
                frozenset(request.allowed_tools) if request.allowed_tools is not None else None
            ),
            owner_type=str(getattr(agent.tools, "owner_type", "main_agent") or "main_agent"),
        )
        return empty, [
            runtime_error_report(exc, context="main_context_bundle.tool_runtime_snapshot")
        ]


# LLM: Routing reads only the owner formal index; missing/invalid authority disables this projection with a typed finding.
# 函数用途: 为当前请求构造 lesson 路由收据，且 Memory 故障不使正常用户任务崩溃。
def _routed_memory_context_for_request(agent, request: RuntimeContextRequest, *, task_local: bool):
    route_mode = str(getattr(agent.config, "memory_rule_routing_mode", "soft") or "soft")
    route_enabled = (
        not task_local
        and bool(getattr(agent.config, "memory_rule_routing_enabled", True))
        and route_mode != "off"
    )
    route_auto_read_limit = int(getattr(agent.config, "memory_rule_auto_read_limit", 3))
    if not route_enabled:
        return RoutedMemoryContext(enabled=False, index_path="")
    try:
        route_root, route_index = runtime_route_root_and_index(agent)
    except (OSError, RuntimeError, ValueError) as exc:
        return RoutedMemoryContext(
            enabled=False,
            index_path="",
            findings=[f"MEMORY_ROUTING_AUTHORITY_UNAVAILABLE:{type(exc).__name__}"],
        )
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


# LLM: Runtime Prompt 只能收到一个 MemoryRecord 列表；routing 原始 Markdown 和 HOT 不得另建注入格式。
# 函数用途: 合并适用的 HOT、已成功路由 lesson 和 active long-term，并清空旧 routing 文本注入。
def _formal_memories_for_request(
    agent,
    request: RuntimeContextRequest,
    routed_context,
    *,
    recall_scope: MemoryRecallScope,
    long_term_memories: list,
    task_local: bool,
) -> list:
    if task_local:
        routed_context.injected_sections = []
        return []
    read_paths = [
        str(receipt.get("path") or "")
        for receipt in list(getattr(routed_context, "receipts", ()) or ())
        if isinstance(receipt, dict) and receipt.get("status") == "read"
    ]
    try:
        hot = hot_memory_records(
            agent.memory_hot,
            agent.memory_lessons,
            scope=recall_scope,
        )
        lessons = routed_lesson_records(
            agent.memory_lessons,
            read_paths=read_paths,
            scope=recall_scope,
            stale_days=float(getattr(agent.config, "home_lesson_stale_caveat_days", 7.0) or 0.0),
        )
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        # Corrupt formal files fail closed for this turn. Doctor/migration exposes the
        # durable repair path; normal user work must not receive partial legacy prose.
        finding = f"formal memory recall unavailable: {type(exc).__name__}"
        if finding not in routed_context.findings:
            routed_context.findings.append(finding)
        hot, lessons = [], []
    routed_context.injected_sections = []
    return _budgeted_formal_memories(_dedupe_formal_memories([*hot, *lessons, *long_term_memories]))


# 记忆注入总预算池(字符≈token,中文 1:1)。上下文有界第一原则:宁可丢不撑爆。
# 各来源已自带上限(HOT 5000 硬顶/long_term top_k 检索/lessons 路由命中),这里是最后保险丝:
# 优先级让位——HOT 全保(最后砍),lessons 次之,long_term 先砍;组内超预算截断尾部。
_FORMAL_MEMORY_BUDGET_CHARS = 10000


def _budgeted_formal_memories(records: list) -> list:
    hot = [record for record in records if str(getattr(record, "kind", "") or "") == "hot"]
    lessons = [
        record for record in records if str(getattr(record, "kind", "") or "") == "lesson"
    ]
    rest = [
        record for record in records
        if str(getattr(record, "kind", "") or "") not in {"hot", "lesson"}
    ]
    kept = list(hot)
    total = sum(len(str(getattr(record, "content", "") or "")) for record in kept)
    for group in (lessons, rest):
        for record in group:
            cost = len(str(getattr(record, "content", "") or ""))
            if kept and total + cost > _FORMAL_MEMORY_BUDGET_CHARS:
                break
            kept.append(record)
            total += cost
    return kept


# LLM: 三个正式来源按稳定 entry_id 精确去重，不做文本相似合并或时间覆盖。
# 函数用途: 保留 HOT、lesson、long-term 的权威优先顺序并去掉重复 ID。
def _dedupe_formal_memories(records: list) -> list:
    result: list = []
    seen: set[str] = set()
    for record in records:
        entry_id = str(getattr(record, "entry_id", "") or "")
        marker = entry_id or f"{getattr(record, 'kind', '')}:{getattr(record, 'content', '')}"
        if marker in seen:
            continue
        seen.add(marker)
        result.append(record)
    return result


def _resume_context_for_request(agent, request: RuntimeContextRequest, *, task_local: bool):
    result = build_auto_resume_context(
        agent,
        request.user_prompt,
        enabled=False if task_local else request.resume_context,
    )
    section = f"### Auto Recovery Context\n{result.context_block}" if result.injected else ""
    return result, section


def _base_runtime_injections(
    request: RuntimeContextRequest, resume_context_section: str, routed_context
):
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


# LLM: 每个 run 在首次模型请求前固定一个工具快照，之后目录、搜索、Schema 与执行只在其上做减法。
# 函数用途: 创建请求级工具事实并驱动压缩和独立工具循环，返回本轮完整运行结果。
def _execute_runtime_loop(agent, params: RuntimeLoopParams):
    write_runtime_fact_start_if_enabled(agent, params)
    audit_source_provision = _provision_audit_sources_before_model(agent, params)
    from ...tooling.models import ToolRuntimeSnapshot
    from ...tooling.runtime_contracts import ToolProtocolSnapshot

    tool_runtime_snapshot = params.tool_runtime_snapshot
    tool_protocol_snapshot = params.tool_protocol_snapshot
    if not isinstance(tool_runtime_snapshot, ToolRuntimeSnapshot):
        raise RuntimeError("tool runtime snapshot is missing from this run")
    if not isinstance(tool_protocol_snapshot, ToolProtocolSnapshot):
        raise RuntimeError("tool protocol snapshot is missing from this run")
    effective_contract_snapshot = _required_action_contract_snapshot(
        agent,
        params,
        tool_runtime_snapshot,
    )
    tool_catalog_section, tool_recommendations_section = _resolve_tool_sections(
        ToolSectionsRequest(
            agent=agent,
            user_prompt=params.user_prompt,
            allowed_tools=params.allowed_tools,
            runtime_snapshot=tool_runtime_snapshot,
            protocol_snapshot=tool_protocol_snapshot,
        )
    )
    compression = _execute_runtime_compression(agent, params)
    loop_params = _tool_loop_execute_params(
        agent,
        RuntimeToolLoopSeed(
            params=params,
            memories=compression.memories,
            tool_catalog_section=tool_catalog_section,
            tool_recommendations_section=tool_recommendations_section,
            tool_runtime_snapshot=tool_runtime_snapshot,
            tool_protocol_snapshot=tool_protocol_snapshot,
            effective_contract_snapshot=effective_contract_snapshot,
        ),
    )
    from ...contracts.required_actions import render_required_action_guidance

    required_guidance = render_required_action_guidance(effective_contract_snapshot)
    if required_guidance:
        loop_params.tool_context.append(required_guidance)
    _queue_audit_source_provision_reply(loop_params, audit_source_provision)
    # 每个 run 都有自己的工具循环状态；同一 owner 的并发聊天/后台轮
    # 不能共享一个 ToolLoopService 实例。
    final_prompt, final_response, tool_rounds = ToolLoopService(agent).execute(loop_params)
    return RuntimeLoopResult(
        final_prompt=final_prompt,
        final_response=final_response,
        tool_rounds=tool_rounds,
        compression_snapshot_id=compression.snapshot_id,
        compression_snapshot_path=compression.snapshot_path,
        compression_applied=compression.applied,
        executed_tools=loop_params.executed_tools,
        archive_tool_calls=loop_params.archive_tool_calls,
        active_turn_user_inputs=list(loop_params.active_turn_user_inputs),
        tool_runtime_evidence=_tool_runtime_evidence(
            tool_runtime_snapshot,
            tool_protocol_snapshot,
            effective_contract_snapshot,
            loop_params.live_archive_state,
            final_response,
        ),
    )


def _tool_runtime_evidence(
    runtime_snapshot: object,
    protocol_snapshot: object,
    contract_snapshot: object,
    live_state: dict[str, object],
    final_response: object,
) -> dict[str, object]:
    capability = getattr(protocol_snapshot, "capability", None)
    actions = tuple(getattr(contract_snapshot, "required_actions", ()) or ())
    assessment = getattr(contract_snapshot, "required_action_assessment", None)
    assessment = assessment if isinstance(assessment, dict) else {}
    action_rows = [item.to_dict() for item in actions if callable(getattr(item, "to_dict", None))]
    final_status = str(getattr(final_response, "runtime_status", "") or "ok")
    open_count = sum(row.get("status") == "open" for row in action_rows)
    return {
        "runtime_snapshot": {
            "run_id": str(getattr(runtime_snapshot, "run_id", "") or ""),
            "snapshot_hash": str(getattr(runtime_snapshot, "snapshot_hash", "") or ""),
            "available_tool_count": len(
                tuple(getattr(runtime_snapshot, "available_tool_names", ()) or ())
            ),
        },
        "protocol": {
            "source_protocol": str(getattr(protocol_snapshot, "source_protocol", "") or ""),
            "provider": str(getattr(capability, "provider", "") or ""),
            "endpoint": str(getattr(capability, "endpoint", "") or ""),
            "model": str(getattr(capability, "model", "") or ""),
            "stream": bool(getattr(capability, "stream", False)),
            "native_supported": bool(getattr(capability, "native_supported", False)),
            "capability_evidence": str(getattr(capability, "evidence", "") or ""),
        },
        "required_action_assessment": {
            "source": str(assessment.get("source") or ""),
            "error": str(assessment.get("error") or ""),
            "requires_action": assessment.get("requires_action"),
        },
        "required_actions": action_rows,
        "tool_choices": [
            dict(item)
            for item in list(live_state.get("tool_choice_trace") or [])
            if isinstance(item, dict)
        ],
        "protocol_violations": [
            dict(item)
            for item in list(live_state.get("protocol_violation_trace") or [])
            if isinstance(item, dict)
        ],
        "completion_gate": {
            "status": final_status,
            "reason": str(getattr(final_response, "runtime_reason", "") or ""),
            "source": str(getattr(final_response, "runtime_source", "") or ""),
            "open_required_action_count": open_count,
            "completed": open_count == 0 and final_status == "ok",
        },
    }


def _provision_audit_sources_before_model(
    agent: object,
    params: RuntimeLoopParams,
) -> dict[str, object]:
    """Reconcile the typed source-worker baseline before the root model runs."""
    from ...ingestion.source_worker import provision_published_audit_source_workers

    provision = provision_published_audit_source_workers(agent)
    if not provision:
        return {}
    params.runtime_injections = [
        *list(params.runtime_injections or []),
        "# Audit Source Provisioning\n"
        "The host has already reconciled the mandatory one-source/one-worker baseline "
        "through the canonical create_subagents lifecycle. Coordinate or inspect these "
        "workers; do not open sources in the root turn and do not duplicate source leaves.\n"
        + json.dumps(provision, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ]
    return provision


def _queue_audit_source_provision_reply(
    params: ToolLoopExecuteParams,
    provision: dict[str, object],
) -> None:
    """Release a fully provisioned detached Audit before any root tool round."""
    if not provision:
        return
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    if str(attrs.get(CONVERSATION_BACKGROUND_EVENT_REASON_ATTR) or "").strip():
        # This continuation exists to report one typed event. Provisioning can
        # still reconcile workers above, but its generic activation receipt
        # must not replace the event turn.
        return
    from ..tool_loop.natural_user_reply import queue_natural_user_reply

    if str(provision.get("error_code") or "") == "AUDIT_NO_PUBLISHED_SOURCES":
        queue_natural_user_reply(
            params,
            kind="named_work_activation_incomplete",
            facts={
                "reply_is_interim": False,
                "task_continues_without_more_user_input": False,
                "current_user_request": params.root_user_prompt or params.user_prompt,
                "named_work": {
                    "work_kind": "audit",
                    "work_name": str(attrs.get("conversation_work_name") or ""),
                    "status": "activation_incomplete",
                    "activation": dict(provision),
                },
            },
        )
        return
    required = max(0, int(provision.get("required") or 0))
    ready = max(0, int(provision.get("ready") or 0))
    if provision.get("ok") is not True or required <= 0 or ready != required:
        return
    queue_natural_user_reply(
        params,
        kind="named_work_active",
        facts={
            "reply_is_interim": True,
            "task_continues_without_more_user_input": True,
            "current_user_request": params.root_user_prompt or params.user_prompt,
            "named_work": {
                "work_kind": "audit",
                "work_name": str(attrs.get("conversation_work_name") or ""),
                "status": "active",
                "source_provision": dict(provision),
            },
        },
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
    return CompressionLoopResult(
        memories=memories, snapshot_id=snapshot_id, snapshot_path=snapshot_path, applied=applied
    )


def _is_task_local_context(value: object) -> bool:
    return str(value or "").strip().lower() in {"task_local", "control_plane"}


def _loop_attempt_id(agent, params: object) -> str:
    """子代理 runner 的 attempt 身份：从当前 runner 上下文取（prepare_runner_attempt
    轮换出的 DB attempt，与并行工具路径 round_execution 同源）优先——RunParams
    构造时已把 attempt_id 兜底为 attempt-{ns} 投影（run_params.py），params 值
    恒非空，放前面会短路掉 runner 上下文；主代理/唤醒轮上下文为空，回落
    params 原值（与既有行为一致）。"""
    from ..runner.context import current_subagent_attempt_id

    return current_subagent_attempt_id(agent) or str(params.attempt_id or "").strip()


def _tool_loop_execute_params(agent, seed: RuntimeToolLoopSeed) -> ToolLoopExecuteParams:
    params = seed.params
    archive_tool_calls: list[dict[str, object]] = list(params.carried_archive_tool_calls or [])
    # H1：compact 自动续跑会重建一个全新的 ToolLoopExecuteParams。除了已重建的 pending_deferred，
    # 还必须从 carried 的 archive 记录里重建这四项运行时状态，否则续跑相当于「失忆重来」：
    #   - one_shot_tool_calls：一次性编排工具（create_subagents/schedule_child_subagents）的去重集合
    #     丢失 → tool_call_runtime 的去重 gate 失效 → 同 payload 续跑会**重复创建子代理**（真副作用）。
    #   - tool_rounds：归零 → max_tool_rounds 预算每次续跑重置 → 长任务可借续跑无限放大工具预算。
    #   - executed_tools：归零 → 重试守卫/完成软提醒看不到历史，会重复劝退或重复读。
    #   - tool_context：归零 → 历史轨迹丢失（_has_previous_tool_context 等守卫失明）。
    # 这是已有 pending_deferred 重建（_live_archive_state_from_carried_archive_tool_calls）的同源补全。
    reconstructed = _reconstructed_runtime_state(archive_tool_calls, agent=agent)
    tool_context: list[str] = reconstructed.tool_context
    active_turn_user_inputs = merge_active_turn_user_inputs(params.carried_active_turn_user_inputs)
    active_turn_user_input_texts_carried = active_turn_user_input_texts(active_turn_user_inputs)
    tool_context.extend(
        f"[ACTIVE_TURN_USER_INPUT]\n{text}" for text in active_turn_user_input_texts_carried
    )
    tool_ir_history: list[object] = []
    if getattr(seed.tool_protocol_snapshot, "source_protocol", "") == "native":
        from ...backends.tool_ir import UserTurn

        tool_ir_history.extend(UserTurn(text) for text in active_turn_user_input_texts_carried)
    tool_rounds = reconstructed.tool_rounds
    one_shot_tool_calls: set[str] = reconstructed.one_shot_tool_calls
    executed_tools: list[str] = reconstructed.executed_tools
    live_archive_state = _live_archive_state_from_carried_archive_tool_calls(archive_tool_calls)
    required_tool_names = {
        name
        for action in tuple(getattr(seed.effective_contract_snapshot, "required_actions", ()) or ())
        if str(getattr(action, "status", "") or "") == "open"
        for name in tuple(getattr(action, "allowed_tools", ()) or ())
    }
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
        attempt_id=_loop_attempt_id(agent, params),
        tool_runtime_snapshot=seed.tool_runtime_snapshot,
        tool_protocol_snapshot=seed.tool_protocol_snapshot,
        effective_contract_snapshot=seed.effective_contract_snapshot,
        tool_rounds=tool_rounds,
        save=params.save,
        live_archive_state=live_archive_state,
        tool_ir_history=tool_ir_history,
        active_turn_user_inputs=active_turn_user_inputs,
        active_turn_transition_callback=params.active_turn_transition_callback,
        context_scope=params.context_scope,
        loaded_tool_names={*reconstructed.loaded_tool_names, *required_tool_names},
        workspace_context_snapshot=_workspace_context_snapshot(agent, params),
        max_protocol_repairs=max(1, int(getattr(getattr(agent, "config", None), "max_protocol_repairs", 2) or 2)),
    )


def _required_action_contract_snapshot(
    agent: object,
    params: RuntimeLoopParams,
    tool_runtime_snapshot: object,
):
    from ...contracts.effective_contract_snapshot import build_effective_contract_snapshot
    from ...contracts.required_actions import (
        RequiredActionAssessment,
        assess_required_actions,
        restore_required_actions_from_records,
    )

    # EXEC-20: cli_run 是任务执行入口, 不存在"信息性陈述"场景——跳过模型预评估
    # 。真机两次实证: 该评估对正常任务消息
    # 误判 requires_action=false → 整轮工具被 no-action-gate 硬拦(TOOL_ACTION_NOT_REQUIRED)
    # → 任务假失败。跳过评估不跳过 required_actions 契约本身: 协作/显式合同路径不变。
    if str(getattr(params, "source", "") or "") == "cli_run":
        assessment = RequiredActionAssessment(
            source="cli_run_task",
            requires_action=True,
        )
    else:
        assessment = assess_required_actions(
            backend=getattr(agent, "backend", None),
            user_prompt=params.root_user_prompt or params.user_prompt,
            runtime_snapshot=tool_runtime_snapshot,
            run_id=params.run_id,
            source_turn_id=params.request_id or params.attempt_id or params.run_id,
            structured_sources=(params.task_attributes, params.delivery_contract),
        )
    metadata = {
        "source": assessment.source,
        "error": assessment.error,
        "raw": assessment.raw,
        "requires_action": assessment.requires_action,
    }
    snapshot = build_effective_contract_snapshot(
        run_id=params.run_id,
        layers=(),
        required_actions=assessment.actions,
        required_action_assessment=metadata,
    )
    restore_required_actions_from_records(
        snapshot,
        params.carried_archive_tool_calls or (),
    )
    return snapshot


def _workspace_context_snapshot(agent, params: RuntimeLoopParams) -> str:
    snapshot = getattr(getattr(agent, "prompts", None), "snapshot_workspace_context", None)
    if not callable(snapshot):
        return ""
    from ...common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    if structured_audit_source_worker_attributes(params.task_attributes):
        return str(snapshot(facts_only=True) or "")
    return str(snapshot() or "")


@dataclass(frozen=True)
class _ReconstructedRuntimeState:
    tool_context: list[str]
    tool_rounds: int
    one_shot_tool_calls: set[str]
    executed_tools: list[str]
    loaded_tool_names: set[str]


def _reconstructed_runtime_state(
    records: list[dict[str, object]], *, agent: object = None
) -> _ReconstructedRuntimeState:
    """从 carried 的 archive 记录重建 compact 续跑要保留的四项运行时状态（H1）。

    - executed_tools：成功且工具名真实（非解析错误/unknown）的记录，按顺序回填工具名——口径
      与 ``_record_tool_call`` 里 ``record.params.executed_tools.append`` 完全一致。
    - one_shot_tool_calls：成功的一次性编排工具记录，用 ``_one_shot_tool_call_key`` 从记录的
      ``parameters``（即原始 payload）回填——与 live 去重 gate（``tool_call_runtime``）同一把钥匙，
      所以 live 会拦的同 payload，续跑也会拦，去重不破。
    - tool_rounds：archive 记录不存轮号，用记录数作保守代理（轮数 ≤ 调用数），保证续跑不把
      ``max_tool_rounds`` 预算清零（宁可略高估、绝不低估，预算只会更紧不会被放大）。
    - tool_context：按 ``_record_tool_call`` 的 ``[tool-record]/[tool-output-record]`` 文本格式
      逐条重建。native 下这段文本**不发往 provider**（builder 旁路，IR messages 才发；这里也
      刻意不重建 ``tool_ir_history``，避免与 IR 双轨冲突、避免把 compact 刚卸掉的历史又塞回原生
      messages），但它仍喂给重试守卫/digest 守卫（``_has_previous_tool_context``）等。文本体量由
      每轮 prompt 构建时既有的 ``window_tool_context_params`` 自动窗口化兜底，不会再撑爆上下文。

    语义摘要增强（短板6）：``tool_context`` 默认仍是逐条机械重建（上面这条契约不变），但当
    ``agent`` 带可用 backend 且配置开启时，会对**中段**记录调一次摘要模型，用一条
    ``[compact-semantic-summary]`` 折叠掉中段、保护首尾——把机械截断换成语义叙述，长任务续跑
    少丢上下文。这是**纯增强**：只动 ``tool_context``（不碰事实源/IR 历史/可恢复性），且摘要
    关闭、记录太少、无 backend、调用失败/超时任意一种都回退到逐条机械列表（行为与改动前一致）。
    """
    valid_records = [record for record in records if isinstance(record, dict)]
    mechanical_entries = [_reconstructed_tool_context_entry(record) for record in valid_records]
    return _ReconstructedRuntimeState(
        tool_context=_tool_context_with_optional_semantic_summary(
            valid_records, mechanical_entries, agent
        ),
        tool_rounds=len(valid_records),
        one_shot_tool_calls={
            key for record in valid_records for key in _carried_one_shot_keys(record)
        },
        executed_tools=[
            name for record in valid_records if (name := _carried_executed_tool_name(record))
        ],
        loaded_tool_names=_pending_carried_loaded_tool_names(valid_records),
    )


def _pending_carried_loaded_tool_names(records: list[dict[str, object]]) -> set[str]:
    """Restore only a tool_search selection not yet consumed by a later model round."""

    rounded = [
        (round_no, record)
        for record in records
        if (round_no := _carried_tool_round(record)) is not None
    ]
    if rounded:
        latest_round = max(round_no for round_no, _ in rounded)
        return {
            name
            for round_no, record in rounded
            if round_no == latest_round
            for name in _carried_loaded_tool_names(record)
        }
    # Legacy carried records did not include a round.  Only a final standalone
    # tool_search can still be known to be pending; never resurrect discoveries
    # from an arbitrary older record.
    return _carried_loaded_tool_names(records[-1]) if records else set()


def _carried_tool_round(record: dict[str, object]) -> int | None:
    try:
        value = int(record.get("tool_round"))
    except (TypeError, ValueError):
        return None
    return max(0, value)


def _carried_loaded_tool_names(record: dict[str, object]) -> set[str]:
    envelope = record.get("tool_result_envelope")
    if not isinstance(envelope, dict):
        return set()
    search = envelope.get("tool_search")
    if not isinstance(search, dict):
        return set()
    names = search.get("loaded_tool_names")
    if not isinstance(names, list):
        return set()
    return {str(item).strip() for item in names if str(item).strip()}


def _tool_context_with_optional_semantic_summary(
    records: list[dict[str, object]], mechanical_entries: list[str], agent: object
) -> list[str]:
    """中段语义摘要的薄接线：可用则折叠中段，否则原样返回机械逐条列表（失败必回退）。

    刻意把决策/调用/兜底全压进 ``memory_archive.compact_semantic_summary``——这里只负责"试一下、
    不行就用机械的"，保证 ``_reconstructed_runtime_state`` 的 H1 契约（tool_rounds/one_shot/
    executed_tools）与机械重建路径在摘要失效时**逐字节不变**。
    """
    if agent is None or not mechanical_entries:
        return mechanical_entries
    from ...memory_archive.compact_semantic_summary import (
        SemanticSummaryRequest,
        semantic_summary_config,
        summarize_carried_tool_context,
    )

    config = semantic_summary_config(agent)
    if not config.enabled:
        return mechanical_entries
    outcome = summarize_carried_tool_context(
        SemanticSummaryRequest(
            records=records,
            mechanical_entries=mechanical_entries,
            config=config,
            backend=getattr(agent, "backend", None),
        )
    )
    if outcome is None:
        return mechanical_entries
    summarized_entries, _stats = outcome
    return summarized_entries


def _carried_executed_tool_name(record: dict[str, object]) -> str:
    """成功且工具名真实的记录返回工具名，否则空串——口径同 ``_record_tool_call``。"""
    if not bool(record.get("ok")):
        return ""
    tool_name = str(record.get("tool") or "").strip()
    return tool_name


def _carried_one_shot_keys(record: dict[str, object]) -> set[str]:
    """成功的一次性编排工具记录返回全部去重 key，否则空集合。"""
    if not bool(record.get("ok")):
        return set()
    params = record.get("parameters")
    if not isinstance(params, dict):
        return set()
    payload = dict(params)
    # archive 的 parameters 即原始 payload（含 "tool" 控制键）；缺失时用记录顶层 tool 兜底，
    # 让 _one_shot_tool_call_keys 能识别这是不是一次性编排工具。
    payload.setdefault("tool", str(record.get("tool") or ""))
    return _one_shot_tool_call_keys(payload)


# LLM: compact 续跑只能从 carried archive 的结构化字段重建，不能从 preview 猜副作用终态。
# 函数用途: 把一条归档工具记录恢复为模型/守卫可读文本，并保留失败、未知与重放事实。
def _reconstructed_tool_context_entry(record: dict[str, object]) -> str:
    payload = record.get("parameters")
    payload = payload if isinstance(payload, dict) else {"tool": str(record.get("tool") or "")}
    status = "ok" if record.get("ok") else "error"
    tool_name = str(record.get("tool") or payload.get("tool") or "unknown")
    payload_lines = "\n".join(
        f"- {key}: {value}"
        for key, value in payload.items()
        if key not in {"tool", "call_id"} and str(value).strip()
    )
    model_summary = str(record.get("model_summary") or "").strip()
    result_lines = [model_summary] if model_summary else [f"[tool={tool_name}; status={status}]"]
    preview = str(record.get("output_preview") or "").strip()
    if preview and not model_summary:
        projected_preview = project_tool_output_body(
            tool=tool_name,
            output=preview,
            trust=str(record.get("tool_output_trust") or "runtime"),
            redaction=str(record.get("tool_output_redaction") or "default"),
        )
        result_lines.append(f"- output_preview: {projected_preview}")
    for key in (
        "scoped_call_id",
        "artifact_ref",
        "output_path",
        "output_hash",
        "error_code",
        "failure_stage",
        "operation_id",
        "tool_operation_status",
        "tool_operation_action",
        "tool_operation_idempotency_scope",
        "tool_operation_reconciliation_source_ref",
        "effect_outcome",
        "effect_source_ref",
    ):
        value = str(record.get(key) or "").strip()
        if value:
            result_lines.append(f"- {key}: {value}")
    result_lines.append(f"- handler_executed: {record.get('handler_executed') is True}")
    result_lines.append(f"- duration_ms: {_nonnegative_tool_duration(record.get('duration_ms'))}")
    if "tool_operation_replayed" in record:
        result_lines.append(
            f"- tool_operation_replayed: {record.get('tool_operation_replayed') is True}"
        )
    return "\n".join(
        [
            f"[tool-record carried tool={tool_name}]",
            payload_lines or f"- tool: {tool_name}",
            "[tool-output-record carried]",
            *result_lines,
        ]
    )


def _nonnegative_tool_duration(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _live_archive_state_from_carried_archive_tool_calls(
    records: list[dict[str, object]],
) -> dict[str, object]:
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
