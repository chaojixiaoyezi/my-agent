# LLM: 装配原运行上下文与宿主回调；展示纯值只经推荐接缝/回调传递，不能写入结果、Agent共享属性或扩大原快照权限。
# 模块用途: 为主链准备记忆、工具和续跑输入，回传本片展示与异常历史；不反向依赖 Gateway。

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass, fields, replace

from ...conversation.active_turn_input import (
    active_turn_user_input_texts,
    merge_active_turn_user_inputs,
)
from ...conversation.authority import (
    CONVERSATION_BACKGROUND_EVENT_REASON_ATTR,
)
from ...memory_archive import build_auto_resume_context
from ...memory_archive.tool_output_externalizer import model_visible_tool_parameters
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
from ...tooling.tool_search_state import pending_carried_loaded_tool_names
from ...user_space.context_bundle import MainContextBundleRequest, build_main_context_bundle
from ...user_space.home_layout import runtime_route_root_and_index
from .._runtime_params import ToolLoopExecuteParams
from .._tool_loop_service import ToolLoopService
from ..parameters import _one_shot_tool_call_keys
from ..tool_context.call_reducer import render_tool_payload_for_live_prompt
from .live_archive import write_runtime_fact_start_if_enabled
from .loop_models import (
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


# LLM: 拒绝列表、展示及已应用Compact视图沿原宿主参数链传递；同片失效不得再决策，批准仍归每次精确调用。
# 函数用途: 把已准备上下文、冻结摘要视图和宿主状态传给公共工具循环，不写盘。
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
        native_compact_carry=params.native_compact_carry,
        conversation_turn_id=params.conversation_turn_id,
        runtime_rejected_actions=params.runtime_rejected_actions,
        active_turn_transition_callback=params.active_turn_transition_callback,
        tool_runtime_snapshot=prepared.tool_runtime_snapshot,
        tool_protocol_snapshot=prepared.tool_protocol_snapshot,
        conversation_history_seed=params.conversation_history_seed,
        compact_context=params.compact_context,
        partial_turn_callback=params.partial_turn_callback,
        capability_presentation=params.capability_presentation,
        capability_presentation_evaluated=params.capability_presentation_evaluated,
        capability_presentation_turn_id=params.capability_presentation_turn_id,
        capability_presentation_callback=params.capability_presentation_callback,
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
        canonical_native_messages=loop_result.canonical_native_messages,
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


# LLM: Runtime recall uses only active long-term plus formal lesson/HOT under structured scope;
# task-local and explicit owner memory-off never read the formal repositories or project index.
# 函数用途: 为一轮主代理请求准备 owner 隔离的 Memory、路由收据、恢复上下文与运行注入，隔离/总闸关闭时跳过正式记忆读取。
def _prepare_runtime_context(agent, request: RuntimeContextRequest):
    tool_runtime_snapshot, tool_protocol_snapshot = _tool_snapshots_for_run(
        agent,
        request,
    )
    task_local = _is_task_local_context(request.context_scope)
    owner_policy = getattr(agent, "owner_policy", None)
    recall_suppressed = task_local or (
        owner_policy is not None and not bool(getattr(owner_policy, "memory_enabled", True))
    )
    memory_top_k = max(1, int(agent.config.memory_top_k or 1))
    routed_context = _routed_memory_context_for_request(
        agent, request, skip_formal_recall=recall_suppressed,
    )
    recall_scope = MemoryRecallScope.from_runtime(
        task_id=request.task_id,
        task_attributes=request.task_attributes,
    )
    # 真机缺口(2026-08-06):普通对话(无 task)的召回只含 global/personal,project 记忆(如
    # 用户喂入的小说知识库,scope=project:novel:xxx)不被召回 → 问小说"信息不足"。这里把
    # 记忆库中实际存在的 project scope 追加进召回范围(用户明确要求记住的项目知识可召回),
    # 有 task 时仍按 task 精确。纯结构化:只扫描 long_term 的 attributes.scope_type/key。
    # gateway 的 request_id 会被当 task_id,故不能以 task_id 判"普通对话"。启用召回时追加
    # 实际存在的 project scope(用户明确要求记住的项目知识可召回),与 task 的 project:task 并存。
    if not recall_suppressed:
        try:
            project_pairs: list[tuple[str, str]] = []
            for record in agent.memory.all():
                attrs = record.attributes if isinstance(record.attributes, dict) else {}
                st = str(attrs.get("scope_type") or "").strip().lower()
                sk = str(attrs.get("scope_key") or "").strip()
                if st == "project" and sk:
                    project_pairs.append(("project", sk))
            if project_pairs:
                recall_scope = MemoryRecallScope(
                    tuple(dict.fromkeys([*recall_scope.keys, *project_pairs]))
                )
        except Exception:
            pass

    long_term_memories = (
        []
        if recall_suppressed
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
        skip_formal_recall=recall_suppressed,
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


# LLM: Routing reads only the owner formal index when formal recall is allowed;
# missing/invalid authority disables this projection with a typed finding.
# 函数用途: 为当前请求构造 lesson 路由收据；隔离或记忆总闸关闭时不读取正式索引。
def _routed_memory_context_for_request(agent, request: RuntimeContextRequest, *, skip_formal_recall: bool):
    route_mode = str(getattr(agent.config, "memory_rule_routing_mode", "soft") or "soft")
    route_enabled = (
        not skip_formal_recall
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


# LLM: 原授权与预算先固定记忆集合；隔离/总闸关闭不读正式库；召回前后两点共用一次原阶段期限。
# 函数用途: 合并 HOT、已路由 lesson 和长期记忆；可选排序与补充只在原权限/预算内生效。
def _formal_memories_for_request(
    agent,
    request: RuntimeContextRequest,
    routed_context,
    *,
    recall_scope: MemoryRecallScope,
    long_term_memories: list,
    skip_formal_recall: bool,
) -> list:
    if skip_formal_recall:
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
    memories = _budgeted_formal_memories(_dedupe_formal_memories([*hot, *lessons, *long_term_memories]))
    from ...conversation.decision_service import begin_decision_stage
    from ...memory_store.decision_recall import (
        _operation_id,
        rerank_recalled_memories,
        supplement_recalled_memories,
        supplemental_query_candidates,
    )

    queries = supplemental_query_candidates(getattr(request, "user_prompt", ""))
    top_k = max(1, int(getattr(agent.config, "memory_top_k", 1) or 1))
    slots = max(0, top_k - len(long_term_memories))
    remaining = _FORMAL_MEMORY_BUDGET_CHARS - sum(len(str(getattr(row, "content", "") or "")) for row in memories)
    post_eligible = sum(str(getattr(row, "kind", "") or "") not in {"hot", "lesson"} for row in memories) >= 2
    pre_eligible = bool(queries and slots and remaining > 0)
    operation = _operation_id(request)
    stage = (begin_decision_stage(agent, request, operation_id=operation)
             if operation and (post_eligible or pre_eligible) else None)

    memories, finding = rerank_recalled_memories(
        agent, request, memories, recall_scope=recall_scope,
        refresh=lambda: _refresh_recall_candidates(agent, memories, read_paths, recall_scope),
        stage=stage,
    )
    if finding:
        routed_context.findings.append(finding)
    if pre_eligible and stage is not None:
        remaining = _FORMAL_MEMORY_BUDGET_CHARS - sum(len(str(getattr(row, "content", "") or "")) for row in memories)
        memories, finding = supplement_recalled_memories(
            agent, request, memories, recall_scope=recall_scope, stage=stage, queries=queries,
            slots=slots, search_top_k=top_k, remaining_chars=remaining,
            refresh=lambda: _refresh_recall_candidates(agent, memories, read_paths, recall_scope),
        )
        if finding:
            routed_context.findings.append(finding)
    return memories


# LLM: 采用旧建议前重读原正式仓库，只投影原选中 ID 并复用原范围/预算；删除、到期或撤销不能被旧排序复活。
# 函数用途: 刷新本批合法候选版本，不重新检索、不增加访问信号、不引入新记忆。
def _refresh_recall_candidates(agent: object, records: list, read_paths: list[str], scope: MemoryRecallScope) -> list:
    available = [
        *hot_memory_records(agent.memory_hot, agent.memory_lessons, scope=scope),
        *routed_lesson_records(agent.memory_lessons, read_paths=read_paths, scope=scope,
                               stale_days=float(getattr(agent.config, "home_lesson_stale_caveat_days", 7.0) or 0.0)),
        *(record for record in agent.memory.all() if long_term_record_matches_scope(record, scope)),
    ]
    current = {record.entry_id: record for record in _dedupe_formal_memories(available)}
    selected = [current[record.entry_id] for record in records if record.entry_id in current]
    return _budgeted_formal_memories(_dedupe_formal_memories(selected))


# 记忆注入总预算池(字符≈token,中文 1:1)。上下文有界第一原则:宁可丢不撑爆。
# 各来源已自带上限(HOT 5000 硬顶/long_term top_k 检索/lessons 路由命中),这里是最后保险丝:
# 优先级让位——HOT 全保(最后砍),lessons 次之,long_term 先砍;组内超预算截断尾部。
_FORMAL_MEMORY_BUDGET_CHARS = 10000


def _budgeted_formal_memories(records: list) -> list:
    hot = [record for record in records if str(getattr(record, "kind", "") or "") == "hot"]
    lessons = [record for record in records if str(getattr(record, "kind", "") or "") == "lesson"]
    rest = [
        record
        for record in records
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


# LLM: 原推荐接缝只采用合法展示；循环可交回已验证 child 的完整协议参数，未交回时沿原初始参数收口，不把活快照写入结果。
# 函数用途: 驱动工具循环；溢出时释放未提交插话并冻结IR，释放失败也沿原异常出口保存已完成事实。
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
    from ...capability.decision_recommendation import recommend_capabilities

    presentation = recommend_capabilities(agent, params, tool_runtime_snapshot, effective_contract_snapshot)
    if callable(params.capability_presentation_callback):
        params.capability_presentation_callback(presentation.selection)
    tool_runtime_snapshot = presentation.tool_snapshot
    tool_catalog_section, tool_recommendations_section = _resolve_tool_sections(
        ToolSectionsRequest(
            agent=agent,
            user_prompt=params.user_prompt,
            allowed_tools=params.allowed_tools,
            runtime_snapshot=tool_runtime_snapshot,
            protocol_snapshot=tool_protocol_snapshot,
        )
    )
    loop_params = _tool_loop_execute_params(
        agent,
        RuntimeToolLoopSeed(
            params=params,
            memories=params.memories,
            tool_catalog_section=tool_catalog_section,
            tool_recommendations_section=tool_recommendations_section,
            tool_runtime_snapshot=tool_runtime_snapshot,
            tool_protocol_snapshot=tool_protocol_snapshot,
            effective_contract_snapshot=effective_contract_snapshot,
            selected_skill_ids=presentation.selected_skill_ids,
            required_skill_ids=presentation.required_skill_ids,
        ),
    )
    _queue_audit_source_provision_reply(loop_params, audit_source_provision)
    # 每个 run 都有自己的工具循环状态；同一 owner 的并发聊天/后台轮
    # 不能共享一个 ToolLoopService 实例。
    service = ToolLoopService(agent)
    try:
        final_prompt, final_response, tool_rounds = service.execute(loop_params)
        loop_params = service.current_params or loop_params
        from ...conversation.compact_carry import capture_native_compact_carry

        native_carry = capture_native_compact_carry(agent, loop_params, final_response)
    except (Exception, KeyboardInterrupt) as exc:
        _persist_partial_native_turn(params.partial_turn_callback, service.current_params or loop_params, exc)
        raise
    return RuntimeLoopResult(
        final_prompt=final_prompt,
        final_response=final_response,
        tool_rounds=tool_rounds,
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        executed_tools=loop_params.executed_tools,
        archive_tool_calls=loop_params.archive_tool_calls,
        active_turn_user_inputs=list(loop_params.active_turn_user_inputs),
        native_compact_carry=native_carry,
        tool_runtime_evidence=_tool_runtime_evidence(
            loop_params.tool_runtime_snapshot,
            loop_params.tool_protocol_snapshot,
            loop_params.effective_contract_snapshot,
            loop_params.live_archive_state,
            final_response,
        ),
        canonical_native_messages=_completed_turn_native_messages(
            loop_params,
            final_response,
        ),
    )


# LLM: 异常快照只含当前 run 的原生 IR；精确会话归属、幂等与写盘修复由原宿主出口负责。
# 函数用途: 在抛回原异常前保存已完成的工具往返；没有原生历史就不生成空的助手回复。
def _persist_partial_native_turn(callback: object, params: ToolLoopExecuteParams, exc: BaseException) -> None:
    from ..models import AgentRunResult

    if not callable(callback):
        return
    native = _completed_turn_native_messages(params, None)
    if not native:
        return
    cancelled = isinstance(exc, (InterruptedError, KeyboardInterrupt))
    result = AgentRunResult(
        prompt="", response="", backend="tool_loop", used_memories=0,
        canonical_native_messages=native,
        archive_tool_calls=list(params.archive_tool_calls),
        executed_tools=list(params.executed_tools),
        runtime_status="cancelled" if cancelled else "failed",
        runtime_reason="user_stop" if cancelled else type(exc).__name__,
        runtime_source="conversation_control" if cancelled else "runtime_error",
        turn_end_reason="aborted" if cancelled else "error",
    )
    try:
        callback(result)
    except Exception:
        # 保存失败不能改写为成功，也不能掩盖原始失败；宿主的原有 repair 仍负责可恢复写盘。
        logging.getLogger(__name__).error("异常回合的原生历史保存失败；请核对会话账本", exc_info=True)


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




def _is_task_local_context(value: object) -> bool:
    return str(value or "").strip().lower() in {"task_local", "control_plane"}


def _loop_attempt_id(agent, params: object) -> str:
    """子代理 runner 的 attempt 身份：从当前 runner 上下文取（prepare_runner_attempt
    轮换出的 DB attempt，与并行工具路径 round_execution 同源）优先——RunParams
    构造时已把 attempt_id 兜底为 attempt-{ns} 投影（run_params.py），params 值
    恒非空，放前面会短路掉 runner 上下文；主代理/唤醒轮上下文为空，回落
    params 原值（与既有行为一致）。"""
    from ...runtime_context import current_subagent_attempt_id

    return current_subagent_attempt_id(agent) or str(params.attempt_id or "").strip()


# LLM: Current-turn IR starts at the exact user task. Completed conversation messages are carried
# separately as provider_history_messages so finalization persists only the new turn. Carried
# handoff/input then append in real chronology.
# 函数用途: 按时间生成当前任务与插话IR；内部ID沿原包保留，供释放核对而不外发。
def _native_initial_tool_ir_history(
    params: RuntimeLoopParams,
    *,
    carried_handoff: str,
    carried_user_inputs: list[str],
) -> list[object]:
    from ...backends.tool_ir import CompactionSummary, UserTurn

    history: list[object] = []
    current = str(params.user_prompt or "")
    if current:
        history.append(UserTurn(_native_user_task_text(current)))
    if carried_handoff:
        history.append(CompactionSummary(carried_handoff))
    packets = merge_active_turn_user_inputs(params.carried_active_turn_user_inputs)
    if packets and len(packets) != len(carried_user_inputs):
        raise ValueError("插话携带包与原生输入数量不一致")
    history.extend(UserTurn(text, input_ids=tuple(packets[index]["input_ids"]) if packets else ())
                   for index, text in enumerate(carried_user_inputs) if str(text or ""))
    return history


# LLM: The transient view must be typed; an arbitrary caller object cannot silently enable
# source hiding or replace a provider-visible summary.
# 函数用途: 校验本片显式Compact载体，缺失时沿原线程路径，不猜测视图。
def _applied_compact_context(value: object):
    from ...conversation.compact_summary_view import AppliedCompactContext

    if value is None:
        return None
    if not isinstance(value, AppliedCompactContext):
        raise TypeError("Compact 应用上下文类型无效")
    return value


# LLM: A narrow history seed may be None even when an applied scoped summary exists. Add that
# summary to current-turn IR after the media-owning initializer, leaving its carried handoff intact.
# 函数用途: 无历史种子时把本次适用摘要插入当前原生回合，保留原交接和媒体块。
def _native_ir_with_applied_summary(
    history: list[object],
    *,
    compact_context: object,
    has_history_seed: bool,
) -> list[object]:
    from ...backends.tool_ir import CompactionSummary, UserTurn

    context = _applied_compact_context(compact_context)
    if context is None or has_history_seed or not context.view.summary.strip():
        return history
    summary = CompactionSummary(
        f"# Earlier Conversation Summary (generation {context.view.generation})\n{context.view.summary}",
        source="applied_compact",
    )
    insert_at = 1 if history and isinstance(history[0], UserTurn) else 0
    history.insert(insert_at, summary)
    return history


# LLM: Explicit applied Compact context selects the initial prefix summary. After a native Compact,
# the new current-turn IR summary owns that view, so callers may omit only this synthetic prefix.
# 函数用途: 生成原生历史，按本次视图选择或省略前置摘要，同时保留原消息与媒体块。
def _native_provider_history_messages(
    params: RuntimeLoopParams | ToolLoopExecuteParams,
    *,
    include_compact_summary: bool = True,
) -> list[dict[str, object]]:
    from ...backends.message_adapter import AnthropicMessageAdapter
    from ...backends.tool_ir import AssistantTurn, CompactionSummary, UserTurn

    seed = params.conversation_history_seed
    if seed is None:
        return []
    prefix_items: list[object] = []
    context = _applied_compact_context(getattr(params, "compact_context", None))
    summary = str(context.view.summary if context is not None else getattr(seed, "compact_summary", "") or "").strip()
    generation = max(0, int(context.view.generation if context is not None else getattr(seed, "compact_generation", 0) or 0))
    if summary and include_compact_summary:
        prefix_items.append(
            CompactionSummary(
                f"# Earlier Conversation Summary (generation {generation})\n{summary}"
            )
        )
    adapter = AnthropicMessageAdapter()
    prefix = adapter.to_provider_messages(prefix_items) if prefix_items else []
    canonical = [
        deepcopy(item)
        for item in tuple(getattr(seed, "canonical_messages", ()) or ())
        if isinstance(item, dict)
    ]
    if canonical:
        return [*prefix, *canonical]
    legacy: list[object] = []
    for item in tuple(getattr(seed, "messages", ()) or ()):
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        role = str(item[0] or "").strip().lower()
        content = str(item[1] or "")
        if role == "user" and content:
            legacy.append(UserTurn(_native_user_task_text(content)))
        elif role == "assistant" and content:
            legacy.append(AssistantTurn(text=content))
    return [*prefix, *adapter.to_provider_messages(legacy)]


# LLM: 原媒体初始化器仍拥有用户IR；以结构类型定位交接并标记source，已覆盖工具隐藏前必须注入同view摘要。
# 函数用途: 为原归档交接标记可替换位置，并把窄历史种子的适用摘要放进原生IR或文本上下文。
def _reconstructed_native_initial_ir(
    agent: object,
    seed: RuntimeToolLoopSeed,
    tool_context: list[str],
    visible_records: list[dict[str, object]],
    carried_user_inputs: list[str],
) -> list[object]:
    if getattr(seed.tool_protocol_snapshot, "source_protocol", "") != "native":
        context = _applied_compact_context(seed.params.compact_context)
        if context is not None and seed.params.conversation_history_seed is None and context.view.summary.strip():
            tool_context.insert(
                0,
                f"# Earlier Conversation Summary (generation {context.view.generation})\n{context.view.summary}",
            )
        return []
    from ...conversation.tool_context_window import native_carried_tool_handoff
    from ...memory_archive.compact_semantic_summary import semantic_summary_config

    handoff = native_carried_tool_handoff(
        tool_context,
        visible_records,
        max_chars=semantic_summary_config(agent).max_input_chars,
    )
    history = _native_initial_tool_ir_history(
        seed.params,
        carried_handoff=handoff,
        carried_user_inputs=carried_user_inputs,
    )
    if handoff:
        from ...backends.tool_ir import CompactionSummary, UserTurn

        index = 1 if history and isinstance(history[0], UserTurn) else 0
        if not isinstance(history[index], CompactionSummary):
            raise TypeError("恢复工具交接位置与原IR构造不一致")
        history[index] = replace(history[index], source="carried_tool_handoff")
    return _native_ir_with_applied_summary(
        history,
        compact_context=seed.params.compact_context,
        has_history_seed=seed.params.conversation_history_seed is not None,
    )


# LLM: Persist only this run's provider-neutral IR plus the terminal assistant response. The
# prior-thread prefix is excluded; ConversationStore and Compact own its lifetime.
# 函数用途: 整理一轮结束后可供下一轮精确回放的原生消息，包括工具调用、结果和最终回复。
def _completed_turn_native_messages(
    params: ToolLoopExecuteParams,
    final_response: object,
) -> list[dict[str, object]]:
    from ...backends.message_adapter import AnthropicMessageAdapter, strip_orphaned_tool_blocks
    from ...backends.tool_ir import AssistantTurn
    from ..native_tool_protocol import native_tool_use_active

    if not native_tool_use_active(params):
        return []
    history = list(getattr(params, "tool_ir_history", None) or [])
    blocks = [
        deepcopy(item)
        for item in list(getattr(final_response, "assistant_content_blocks", None) or [])
        if isinstance(item, dict)
    ]
    has_tool_use = any(str(item.get("type") or "") == "tool_use" for item in blocks)
    final_text = str(getattr(final_response, "text", "") or "")
    if not has_tool_use and (final_text or blocks):
        history.append(AssistantTurn(text=final_text, content_blocks=blocks))
    return strip_orphaned_tool_blocks(AnthropicMessageAdapter().to_provider_messages(history))


# LLM: Historical and current ordinary user turns use one deterministic provider representation;
# do not mix recalled memory, workspace clocks, or tool recommendations into this cache identity.
# 函数用途: 给普通用户消息加固定任务标题，使下一轮重建出的历史与上一轮缓存前缀完全一致。
def _native_user_task_text(value: object) -> str:
    return f"# User Task\n{str(value or '')}"


# LLM: Resume reconstruction has two projections: the complete owner archive restores budgets,
# dedupe and effect state, while the explicit applied view hides only its own source refs.
# Never use the bounded model projection as runtime authority. Exact rejection memory
# remains the same host-owned list across automatic continuations, independent of model history.
# Skill presentation travels with this seed only; it cannot mutate the original grant or loaded-tool facts.
# 函数用途: 从新权限快照及完整归档恢复执行状态；同进程carry保留原IR，不重跑归档摘要或用户轮初始化。
def _tool_loop_execute_params(agent, seed: RuntimeToolLoopSeed) -> ToolLoopExecuteParams:
    params = seed.params
    from ...backends.tool_ir import CompactionSummary
    from ...conversation.compact_carry import restore_native_compact_carry

    native_carry = restore_native_compact_carry(agent, params)
    archive_tool_calls: list[dict[str, object]] = list(params.carried_archive_tool_calls or [])
    from ...conversation.active_turn_compact import model_visible_active_turn_tool_calls

    model_visible_archive_tool_calls = model_visible_active_turn_tool_calls(
        agent,
        params.task_attributes,
        archive_tool_calls,
        compact_context=params.compact_context,
    )
    # H1：compact 自动续跑会重建一个全新的 ToolLoopExecuteParams。除了已重建的 pending_deferred，
    # 还必须从 carried 的 archive 记录里重建这四项运行时状态，否则续跑相当于「失忆重来」：
    #   - one_shot_tool_calls：一次性编排工具（create_subagents）的去重集合
    #     丢失 → tool_call_runtime 的去重 gate 失效 → 同 payload 续跑会**重复创建子代理**（真副作用）。
    #   - tool_rounds：归零 → max_tool_rounds 预算每次续跑重置 → 长任务可借续跑无限放大工具预算。
    #   - executed_tools：归零 → 重试守卫/完成软提醒看不到历史，会重复劝退或重复读。
    #   - tool_context：归零 → 历史轨迹丢失（_has_previous_tool_context 等守卫失明）。
    # 这是已有 pending_deferred 重建（_live_archive_state_from_carried_archive_tool_calls）的同源补全。
    reconstructed = _reconstructed_runtime_state(
        archive_tool_calls,
        model_visible_records=[] if native_carry is not None else model_visible_archive_tool_calls,
        agent=None if native_carry is not None else agent,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
    )
    tool_context: list[str] = list(native_carry.tool_context) if native_carry is not None else reconstructed.tool_context
    active_turn_user_inputs = merge_active_turn_user_inputs(params.carried_active_turn_user_inputs)
    active_turn_user_input_texts_carried = active_turn_user_input_texts(active_turn_user_inputs)
    tool_ir_history = list(native_carry.history) if native_carry is not None else _reconstructed_native_initial_ir(
        agent, seed, tool_context, model_visible_archive_tool_calls,
        active_turn_user_input_texts_carried,
    )
    if native_carry is None:
        tool_context.extend(f"[ACTIVE_TURN_USER_INPUT]\n{text}" for text in active_turn_user_input_texts_carried)
    tool_rounds = reconstructed.tool_rounds
    one_shot_tool_calls: set[str] = reconstructed.one_shot_tool_calls
    executed_tools: list[str] = reconstructed.executed_tools
    live_archive_state = _live_archive_state_from_carried_archive_tool_calls(archive_tool_calls)
    if native_carry is not None:
        live_archive_state["_forwarded_runtime_guidance"] = set(native_carry.forwarded_guidance)
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
        selected_skill_ids=seed.selected_skill_ids,
        required_skill_ids=seed.required_skill_ids,
        tool_rounds=tool_rounds,
        save=params.save,
        live_archive_state=live_archive_state,
        tool_ir_history=tool_ir_history,
        conversation_turn_id=params.conversation_turn_id,
        provider_history_messages=_native_provider_history_messages(params, include_compact_summary=not any(
            isinstance(item, CompactionSummary) and item.source == "applied_compact" for item in tool_ir_history)),
        active_turn_user_inputs=active_turn_user_inputs,
        active_turn_transition_callback=params.active_turn_transition_callback,
        runtime_rejected_actions=params.runtime_rejected_actions,
        conversation_history_seed=params.conversation_history_seed,
        compact_context=params.compact_context,
        context_scope=params.context_scope,
        loaded_tool_names={*reconstructed.loaded_tool_names, *required_tool_names},
        workspace_context_snapshot=_workspace_context_snapshot(agent, params),
        max_protocol_repairs=max(
            1, int(getattr(getattr(agent, "config", None), "max_protocol_repairs", 2) or 2)
        ),
    )


def _required_action_contract_snapshot(
    agent: object,
    params: RuntimeLoopParams,
    tool_runtime_snapshot: object,
):
    from ...contracts.effective_contract_snapshot import build_effective_contract_snapshot

    # LLM: 会话运行时 式工具循环不在 turn 前后生成或追踪“必做动作”；
    # 安全仍由工具 schema/权限/未知副作用闸保护，这个空快照只保持现有调用接口。
    # 函数用途: 为工具运行时创建不携带机器完成义务的请求快照。
    del agent, tool_runtime_snapshot
    return build_effective_contract_snapshot(
        run_id=params.run_id,
        layers=(),
        required_actions=(),
        required_action_assessment={"source": "disabled", "requires_action": None},
    )


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


# LLM: This container deliberately keeps effect-control fields separate from model-facing context.
# 类用途: 保存跨工作片重建出的工具预算、去重事实、已执行工具和可见摘要。
@dataclass(frozen=True)
class _ReconstructedRuntimeState:
    tool_context: list[str]
    tool_rounds: int
    one_shot_tool_calls: set[str]
    executed_tools: list[str]
    loaded_tool_names: set[str]


# LLM: Full records restore tool rounds, one-shot keys, executed names and loaded tools. The optional
# model-visible subset may affect only tool_context, so Compact cannot reset budgets or replay effects.
# 函数用途: 从完整工具账恢复运行控制状态，同时可用已压缩后的子集生成模型可见历史。
def _reconstructed_runtime_state(
    records: list[dict[str, object]],
    *,
    model_visible_records: list[dict[str, object]] | None = None,
    agent: object = None,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
) -> _ReconstructedRuntimeState:
    """从 carried 的 archive 记录重建 compact 续跑要保留的四项运行时状态（H1）。

    - executed_tools：成功且工具名真实（非解析错误/unknown）的记录，按顺序回填工具名——口径
      与 ``_record_tool_call`` 里 ``record.params.executed_tools.append`` 完全一致。
    - one_shot_tool_calls：成功的一次性编排工具记录，用 ``_one_shot_tool_call_key`` 从记录的
      ``model_parameters``（provider 原始 payload）回填——与 live 去重 gate
      （``tool_call_runtime``）同一把钥匙，
      所以 live 会拦的同 payload，续跑也会拦，去重不破。
    - tool_rounds：archive 记录不存轮号，用记录数作保守代理（轮数 ≤ 调用数），保证续跑不把
      ``max_tool_rounds`` 预算清零（宁可略高估、绝不低估，预算只会更紧不会被放大）。
    - tool_context：按 ``_record_tool_call`` 的 ``[tool-record]/[tool-output-record]`` 文本格式
      逐条重建，继续喂给重试守卫/digest 守卫（``_has_previous_tool_context``）等。native 不能从
      跨进程索引伪造原始 AssistantTurn/ToolCall/ToolResult 配对，但会把同一批已脱敏记录投影为唯一
      ``CompactionSummary`` handoff，随 IR 在后续 provider 请求持续可见；精确副作用仍由 archive、
      operation ledger 和 artifact refs 掌权。这既避免双轨重放，也不再让 lifecycle wake 丢掉本轮
      已执行历史。文本体量与 handoff 共同复用既有 Compact 配置和窗口化边界。

    语义摘要增强（短板6）：``tool_context`` 默认仍是逐条机械重建（上面这条契约不变），但当
    ``agent`` 带可用 backend 且配置开启时，会对**中段**记录调一次摘要模型，用一条
    ``[compact-semantic-summary]`` 折叠掉中段、保护首尾——把机械截断换成语义叙述，长任务续跑
    少丢上下文。这是**纯增强**：只动重建投影（不碰事实源/可恢复性），native 再把该投影作为
    一条 CompactionSummary 放回同一 IR。摘要关闭、记录太少、无 backend、调用失败/超时任意一种
    都回退到逐条机械列表（行为与改动前一致）。
    """
    valid_records = [record for record in records if isinstance(record, dict)]
    visible_records = (
        valid_records
        if model_visible_records is None
        else [record for record in model_visible_records if isinstance(record, dict)]
    )
    mechanical_entries = [_reconstructed_tool_context_entry(record) for record in visible_records]
    return _ReconstructedRuntimeState(
        tool_context=_tool_context_with_optional_semantic_summary(
            visible_records,
            mechanical_entries,
            agent,
            request_id=request_id,
            run_id=run_id,
            task_id=task_id,
        ),
        tool_rounds=len(valid_records),
        one_shot_tool_calls={
            key for record in valid_records for key in _carried_one_shot_keys(record)
        },
        executed_tools=[
            name for record in valid_records if (name := _carried_executed_tool_name(record))
        ],
        loaded_tool_names=pending_carried_loaded_tool_names(valid_records),
    )


# LLM: Recovery Compact needs the exact same bounded model projection as the next ToolLoop seed,
# while runtime dedupe/tool-round state remains private to _reconstructed_runtime_state.
# 函数用途: 为跨工作片 Compact 生成与下一轮一致的工具文本投影，不改变完整工具账或运行预算。
def reconstructed_model_tool_context(
    records: list[dict[str, object]],
    *,
    agent: object = None,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
) -> list[str]:
    return _reconstructed_runtime_state(
        records,
        model_visible_records=records,
        agent=agent,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
    ).tool_context


def _tool_context_with_optional_semantic_summary(
    records: list[dict[str, object]],
    mechanical_entries: list[str],
    agent: object,
    *,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
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
            agent=agent,
            request_id=request_id,
            run_id=run_id,
            task_id=task_id,
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


# LLM: Rebuild one-shot dedupe keys from provider-authored arguments only; trusted/default host
# completion fields are execution facts and cannot change the model-intent identity.
# 函数用途: 从续跑归档恢复一次性编排工具的原始意图去重键。
def _carried_one_shot_keys(record: dict[str, object]) -> set[str]:
    """成功的一次性编排工具记录返回全部去重 key，否则空集合。"""
    if not bool(record.get("ok")):
        return set()
    params = model_visible_tool_parameters(record)
    if not params:
        return set()
    payload = dict(params)
    # model_parameters 是 provider 原始 payload；旧索引仅按结构化 input_sources 回退。
    # 缺失时用记录顶层 tool 兜底，让去重键与 live 模型调用保持同一口径。
    payload.setdefault("tool", str(record.get("tool") or ""))
    return _one_shot_tool_call_keys(payload)


# LLM: Compact continuation may rebuild only typed archive fields. Reuse the live prompt payload
# reducer after redaction so large write bodies stay behind hashes/previews instead of being copied
# verbatim into every background slice; effect state still comes only from typed archive fields.
# 函数用途: 把一条归档工具记录脱敏、限长后恢复为模型/守卫可读文本，并保留路径、失败、未知与重放事实。
def _reconstructed_tool_context_entry(record: dict[str, object]) -> str:
    payload = model_visible_tool_parameters(record)
    payload = payload or {"tool": str(record.get("tool") or "")}
    from ...common.log_redaction import redact_sensitive_value

    projected_payload = redact_sensitive_value(payload)
    payload = projected_payload if isinstance(projected_payload, dict) else {}
    status = "ok" if record.get("ok") else "error"
    tool_name = str(record.get("tool") or payload.get("tool") or "unknown")
    payload.setdefault("tool", tool_name)
    payload_lines = render_tool_payload_for_live_prompt(payload)
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
