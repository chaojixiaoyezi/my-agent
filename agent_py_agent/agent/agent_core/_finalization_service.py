# LLM: 收口沿原模型账本和会话领域；用量事件身份由原 store 按范围/快照派生，禁止依赖物理调用数代替快照。
# 模块用途: 保存运行结果、归档与用量；原后台认证义务在任务终结前冻结，不拥有第二份模型或通知账。
from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from pathlib import Path

from ..common.logical_reference_ids import RUN_REQUEST_ID_PREFIX
from ..conversation.task_state import conversation_task_completed
from ..memory_archive import (
    archive_run_turn,
    estimate_tokens,
)
from ..memory_archive.runtime.turn_archiver import ArchiveRunTurnParams, ArchiveTurnContext
from ..memory_archive.runtime_fact_source import RuntimeFactSourceRequest, write_runtime_fact_source
from ..memory_archive.tokens import TurnTokenUsage, append_session_token_usage
from ..prompting_parts.runtime_injection import runtime_injection_texts
from ..tooling.operation_verification import (
    build_operation_verification,
    redact_executed_operation_labels,
)
from ..turn_end import TurnEndReason, infer_turn_end_reason
from ..user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    update_main_context_bundle_artifacts,
)
from ._runtime_params import (
    ArchiveRunParams,
    EstimateTokenParams,
    FinalizeContext,
)
from .finalization_compact_auto import compact_auto_cycle_fields
from .model.usage import input_token_usage, output_token_usage
from .models import AgentRunResult
from .run_task_workspace_writer import (
    write_run_task_workspace_if_needed,
)
from .runtime.live_archive import ARCHIVE_PREVIEW_LIMITS, ARCHIVE_SUMMARY_CHARS
from .runtime.owner_roots import runtime_archive_roots


@dataclass(frozen=True)
class BuildAgentRunResultParams:
    ctx: FinalizeContext
    archive_result: object
    token_ledger: dict[str, int]
    run_request_id: str
    model_calls: dict[str, object]


# LLM: 结果、归档、用量各走原事实源；修改收口顺序须同步运行失败和辅助调用测试。
# 类用途: 汇总一次主代理运行的最终保存动作，不把费用显示当作业务完成依据。
class FinalizationService:
    def __init__(self, agent):
        self._agent = agent

    # LLM: 原后台首次认证拒绝在关任务前冻结同wake义务；交付确认前不关闭active TaskLink，不复活终态，其余归档/用量/记忆合同不变。
    #   已采纳同源正文先交原显示出口；原取消/拒绝或改写正文不从blocks复活，联测阶段投影与partial持久化。
    # 函数用途: 收口模型事实与可见消息，保留通知资格，再按真实终态登记任务和原后台学习请求。
    def finalize(self, ctx: FinalizeContext):
        assert ctx.final_response is not None
        from .model.visible_response import commit_visible_response

        commit_visible_response(ctx, ctx.final_response, boundary="terminal")
        from ..conversation.technical_continuation import record_closeout
        cutoff_receipt = record_closeout(self._agent, ctx)
        from ..conversation.required_user_action import defer_required_action_completion
        if _conversation_turn_is_terminal(ctx) and not defer_required_action_completion(self._agent, ctx):
            # 会话运行时 的普通 turn 以运行时最终响应事件结束；不解析“做完了”等自然语言，
            # 也不再扫描 output/ 或要求模型额外提交验收。
            from ..conversation.business_contracts import BusinessExecutionContext
            from ..conversation.task_promotion import (
                complete_current_conversation_task,
                promote_conversation_task_for_run,
            )

            current = getattr(self._agent, "_current_run_params", None)
            # 原受管订单即使无需工具，也须在同轮收口前建立原TaskLink；不依赖可关闭的运行归档。
            if isinstance(getattr(current, "business_context", None), BusinessExecutionContext):
                promote_conversation_task_for_run(self._agent, current)
            complete_current_conversation_task(
                self._agent,
                ctx.task_attributes,
                source=ctx.source,
                current_task_id=ctx.task_id,
            )
        run_request_id = ctx.request_id or f"{RUN_REQUEST_ID_PREFIX}{time_module.time_ns()}"
        _freeze_turn_asset_mutations(self._agent, ctx)

        archive_params = ArchiveRunParams(
            do_save=ctx.do_save,
            user_prompt=ctx.user_prompt,
            final_response=ctx.final_response,
            archive_tool_calls=ctx.archive_tool_calls,
            run_request_id=run_request_id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            source=ctx.source,
            task_attributes=ctx.task_attributes,
            context_scope=ctx.context_scope,
        )
        archive_result = self._archive_run_if_needed(archive_params)
        model_calls = _current_model_call_summary(
            self._agent,
            ctx,
            run_request_id,
        )
        self._write_runtime_fact_source_if_needed(ctx, run_request_id, model_calls)
        self.settle_model_usage(ctx, model_calls=model_calls)
        self._update_main_context_bundle_artifacts(ctx, run_request_id)
        token_ledger = self._estimate_token_usage(_estimate_token_params(ctx, run_request_id))

        result = self._build_agent_run_result(
            BuildAgentRunResultParams(
                ctx,
                archive_result,
                token_ledger,
                run_request_id,
                model_calls,
            )
        )
        if cutoff_receipt is None or cutoff_receipt.status not in {"pending", "stopped"}:
            _schedule_typed_unfinished_continuation(self._agent, ctx)
        from .runtime.goal_accounting import finish_goal_turn_accounting

        finish_goal_turn_accounting(self._agent, ctx.task_attributes)
        if (conversation_task_completed(ctx.task_attributes)
                and 'query_failed' not in ctx.tool_runtime_evidence.get('evidence_tags', [])):
            _request_memory_curator(self._agent, "task_complete")
        if (conversation_task_completed(ctx.task_attributes) or ctx.source == "background_main_agent"
                or ctx.tool_runtime_evidence.get('asset_evolution_run_id')):
            _request_skill_learning(self._agent, ctx)
        _reclaim_turn_pty_sessions(self._agent, ctx)
        return result

    def _write_runtime_fact_source_if_needed(
        self,
        ctx: FinalizeContext,
        run_request_id: str,
        model_calls: dict[str, object],
    ) -> str:
        if not ctx.do_save:
            return ""
        written = ""
        for root in runtime_archive_roots(
            self._agent,
            context_scope=ctx.context_scope,
            task_attributes=ctx.task_attributes,
        ):
            written = write_runtime_fact_source(
                RuntimeFactSourceRequest(
                    root=root,
                    request_id=run_request_id,
                    user_prompt=ctx.user_prompt,
                    response_text=getattr(ctx.final_response, "internal_response", "") or ctx.final_response.text,
                    backend=ctx.final_response.backend,
                    status=str(getattr(ctx.final_response, "runtime_status", "ok") or "ok"),
                    next_actions=ctx.recovery_next_actions or [],
                    archive_tool_calls=ctx.archive_tool_calls or [],
                    runtime_injections=runtime_injection_texts(ctx.runtime_injections),
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                    source=ctx.source,
                    phase="final",
                    tool_rounds=ctx.tool_rounds,
                    executed_tools=list(ctx.executed_tools or []),
                    latest_archive_refs=_latest_archive_refs(ctx.archive_tool_calls or []),
                    artifact_refs=_artifact_refs(ctx.archive_tool_calls or []),
                    delivery_contract=ctx.delivery_contract,
                    model_calls=model_calls,
                )
            )
        return written

    # LLM: 成功/错误/取消按 exact thread/request/run 提交原累计账；store 以范围和完整快照去重，迟到尝试不冲突。
    # 函数用途: 幂等保存本会话的模型用量，同物理调用数的新事实仅补增量，前后台交接不重复计算。
    def settle_model_usage(
        self,
        ctx: object,
        *,
        model_calls: dict[str, object] | None = None,
    ) -> str:
        request_id = str(ctx.request_id or "").strip()
        if model_calls is None:
            model_calls = _current_model_call_summary(self._agent, ctx, request_id)
        if int(model_calls.get("physical_model_attempt_count") or 0) <= 0:
            return ""
        attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
        thread_id = str(
            attrs.get("agent_thread_id")
            or attrs.get("conversation_thread_id")
            or ""
        ).strip()
        store = getattr(self._agent, "conversation_store", None)
        append_usage = getattr(getattr(store, "model_usage", None), "append_snapshot_once", None)
        if not thread_id or not callable(append_usage):
            return ""
        event = append_usage(
            {
                "thread_id": thread_id,
                "request_id": request_id,
                "run_id": str(ctx.run_id or "").strip(),
                "task_id": str(ctx.task_id or "").strip(),
                "source": str(ctx.source or "").strip(),
                "model_calls": model_calls,
                "now": time_module.time(),
            }
        )
        return str(getattr(event, "event_id", "") or "")

    def _update_main_context_bundle_artifacts(
        self, ctx: FinalizeContext, run_request_id: str
    ) -> None:
        if not ctx.do_save or not ctx.main_context_bundle_path:
            return
        update_main_context_bundle_artifacts(
            MainContextBundleArtifactUpdateRequest(
                context_bundle_path=ctx.main_context_bundle_path,
                workspace_root=self._agent.root,
                request_id=ctx.request_id or run_request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
            )
        )

    # LLM: 运行归档仅保存经历引用，正文不在此晋升；宿主thread身份必须完整透传并保持main后台收口。
    # 函数用途: 按保存开关写任务工作区和运行经历档案，不做长期事实晋升。
    # LLM: 终态归档只从宿主task_attributes传线程；保留main的save、任务工作区、公开回执与后台通知收口。
    # 函数用途: 在原归档路径保存回合事实，并为Curator保留精确来源关联。
    def _archive_run_if_needed(self, params: ArchiveRunParams):
        if not params.do_save:
            return None
        write_run_task_workspace_if_needed(self._agent, params)
        result = None
        for root in runtime_archive_roots(
            self._agent,
            context_scope=params.context_scope,
            task_attributes=params.task_attributes,
        ):
            result = archive_run_turn(
                ArchiveRunTurnParams(
                    root=root,
                    ctx=ArchiveTurnContext(
                        session_id=getattr(
                            self._agent, "session_id", self._agent.config.agent_name
                        ),
                        request_id=params.run_request_id,
                        thread_id=str((params.task_attributes or {}).get("conversation_thread_id") or ""),
                        run_id=params.run_id,
                        task_id=params.task_id,
                        user_prompt=params.user_prompt,
                        response_text=params.final_response.text,
                        backend=params.final_response.backend,
                        tool_calls=params.archive_tool_calls or [],
                        source=params.source,
                        archive_level=int(getattr(self._agent.config, "memory_archive_level", 3)),
                        preview_limits=dict(ARCHIVE_PREVIEW_LIMITS),
                        summary_chars=ARCHIVE_SUMMARY_CHARS,
                    ),
                ),
            )
        return result

    def _estimate_token_usage(self, params: EstimateTokenParams):
        input_tokens = input_token_usage(params.final_response)
        prompt_tokens = estimate_tokens(params.final_prompt) if params.final_prompt else 0
        if input_tokens is None:
            if params.final_prompt:
                input_tokens = prompt_tokens
            else:
                input_tokens = (
                    estimate_tokens(params.user_prompt)
                    + estimate_tokens(list(runtime_injection_texts(params.runtime_injections)))
                    + estimate_tokens(
                        [getattr(memory, "content", "") for memory in params.memories]
                    )
                )
        output_tokens = output_token_usage(params.final_response)
        if output_tokens is None:
            output_tokens = estimate_tokens(params.final_response.text)
        tool_tokens = estimate_tokens(params.archive_tool_calls)
        ledger = {}
        for root in runtime_archive_roots(
            self._agent,
            context_scope=params.context_scope,
            task_attributes=params.task_attributes,
        ):
            ledger = append_session_token_usage(
                root,
                usage=TurnTokenUsage(
                    session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                    turn_id=params.turn_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    tool_tokens=tool_tokens,
                    created_at=str(time_module.time()),
                ),
            )
        return {
            "turn": int(ledger["turn_total"]),
            "cumulative": int(ledger["cumulative_tokens"]),
            "active": _active_context_tokens(input_tokens, output_tokens, prompt_tokens),
        }

    # LLM: response保内部最终段、model_response保整次当前text；公开净化归显示/投递门，只有结构化commentary-only缺候选才改缺终答状态。
    # 函数用途: 组装内部运行结果与宿主账，保原无phase正文/归档及全部拒绝、超时转换后的当前text权威。
    def _build_agent_run_result(self, params: BuildAgentRunResultParams):
        from .model.response_provenance import response_provenance_projection

        ctx = params.ctx
        routed_context = ctx.routed_context
        operation_verification = build_operation_verification(
            self._agent,
            ctx.archive_tool_calls,
        )
        model_calls = params.model_calls
        runtime_status = str(getattr(ctx.final_response, "runtime_status", "ok") or "ok")
        runtime_reason = str(getattr(ctx.final_response, "runtime_reason", "") or "")
        runtime_source = str(getattr(ctx.final_response, "runtime_source", "") or "")
        turn_end_reason = infer_turn_end_reason(
            explicit=getattr(ctx.final_response, "turn_end_reason", ""),
            runtime_status=runtime_status,
            runtime_reason=runtime_reason,
            stop_reason=getattr(ctx.final_response, "stop_reason", ""),
        )
        ctx.final_response.turn_end_reason = turn_end_reason
        # LLM: 会话运行时 式自然收口保留模型原文；宿主只单独记录 turn_end_reason，
        # 不再把机器验收结论包装成“任务未完成”用户文案。
        honest_text = str(ctx.final_response.text or "").rstrip()
        from ..backends.assistant_messages import commentary_only_response
        from .model.visible_response import accepted_final_text

        final_text = accepted_final_text(ctx, ctx.final_response, project=lambda text: text).rstrip()
        if honest_text and not final_text and runtime_status == "ok" and commentary_only_response(ctx.final_response):
            runtime_status, runtime_reason, runtime_source = "user_reply_unavailable", "USER_REPLY_UNAVAILABLE", "tool_loop"
        delivery_artifacts = _structured_delivery_artifacts(ctx, agent=self._agent)
        delivery_diagnostics = (ctx.task_attributes or {}).get('child_delivery_diagnostics', [])
        return AgentRunResult(
            prompt=ctx.final_prompt,
            response=redact_executed_operation_labels(
                final_text,
                operation_verification,
            ),
            backend=ctx.final_response.backend,
            used_memories=len(ctx.memories),
            internal_response=str(getattr(ctx.final_response, "internal_response", "") or ""),
            public_receipt=getattr(ctx.final_response, "public_receipt", None),
            final_response_provenance=response_provenance_projection(ctx.final_response),
            model_response=honest_text,
            user_reply_diagnostic=getattr(ctx.final_response, "user_reply_diagnostic", None),
            assistant_commentary_messages=_assistant_commentary_messages(ctx.on_chunk),
            tool_rounds=ctx.tool_rounds,
            executed_tools=ctx.executed_tools,
            archive_tool_calls=ctx.archive_tool_calls,
            memory_route_matches=len(routed_context.matches),
            memory_route_paths=[
                *routed_context.required_read_paths,
                *routed_context.candidate_paths,
            ],
            archive_events=params.archive_result.event_count if params.archive_result else 0,
            archive_token_estimate=params.archive_result.token_estimate
            if params.archive_result
            else 0,
            prompt_token_estimate=estimate_tokens(ctx.final_prompt),
            runtime_injection_token_estimate=estimate_tokens(list(runtime_injection_texts(ctx.runtime_injections)))
            if ctx.runtime_injections
            else 0,
            **_snapshot_result_fields(),
            **_resume_context_fields(ctx),
            compression_snapshot_id=ctx.compression_snapshot_id,
            compression_snapshot_path=ctx.compression_snapshot_path,
            compression_applied=ctx.compression_applied,
            turn_token_estimate=params.token_ledger["turn"],
            cumulative_token_estimate=params.token_ledger["cumulative"],
            **_model_call_result_fields(model_calls),
            main_context_bundle_path=ctx.main_context_bundle_path,
            main_context_bundle_markdown_path=ctx.main_context_bundle_markdown_path,
            runtime_status=runtime_status,
            runtime_reason=runtime_reason,
            runtime_source=runtime_source,
            turn_end_reason=turn_end_reason,
            conversation_task_completed=conversation_task_completed(ctx.task_attributes),
            delivery_artifacts=delivery_artifacts,
            delivery_artifact_diagnostics=list(delivery_diagnostics) if isinstance(delivery_diagnostics, list) else [],
            message_tool_deliveries=_message_tool_deliveries(ctx, agent=self._agent),
            operation_verification=operation_verification,
            tool_runtime_evidence=_finalization_learning_evidence(self._agent, ctx),
            pack_verifications=_pack_verification_facts(self._agent, ctx),
            active_turn_user_inputs=list(ctx.active_turn_user_inputs or []),
            canonical_native_messages=list(ctx.canonical_native_messages or []),
            **compact_auto_cycle_fields(
                self._agent, ctx, params.token_ledger, request_id=params.run_request_id,
                archive_result=params.archive_result,
            ),
        )


# LLM: Commentary authority comes only from a sink method that records real tool boundaries;
# arbitrary callbacks and model text cannot opt themselves into durable conversation history.
# 函数用途: 从本轮流接收器读取按顺序确认的助手过程消息，缺少能力时返回空列表。
def _assistant_commentary_messages(on_chunk: object) -> list[str]:
    reader = getattr(on_chunk, "assistant_commentary_messages", None)
    if not callable(reader):
        return []
    try:
        values = reader()
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    if not isinstance(values, (list, tuple)):
        return []
    return [
        text
        for value in values
        if (text := str(value or "").strip())
    ]


# LLM: finalization only submits a typed reason to the one Curator state; it never runs a
# background model inline or writes daily/candidate/long-term data itself.
# 函数用途: 在任务真正终态后 best-effort 登记异步记忆提炼请求，失败不影响用户回复。
def _request_memory_curator(agent: object, reason: str) -> None:
    curator = getattr(agent, "memory_curator", None)
    request = getattr(curator, "request", None)
    if not callable(request):
        return
    try:
        request(reason)
    except Exception:
        return


# LLM: 收口只冻结原run/attempt中已验证字节，不调用模型；无权限、关闭开关或task_local均不生成请求。
# 函数用途: 将临时变更账原子冻结为后台演进材料。
def _freeze_turn_asset_mutations(agent, ctx):
    states = getattr(agent, '_asset_mutation_states', {})
    state = states.pop((ctx.run_id, getattr(ctx, 'attempt_id', '')), {})
    if (not getattr(getattr(agent, 'config', None), 'enable_self_learning', False)
            or not ctx.do_save or ctx.context_scope == 'task_local'):
        return
    from ..assets.mutation_ledger import freeze_asset_mutations, query_analysis_blocked

    attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
    if any(query_analysis_blocked(row) for row in ctx.archive_tool_calls or ()):
        state.setdefault('asset_mutations', {})['evidence_tags'] = ['query_failed']
    if state.get('asset_mutations', {}).get('evidence_tags'):
        ctx.tool_runtime_evidence['evidence_tags'] = ['query_failed']
    try:
        request = freeze_asset_mutations(agent.home_paths.owner_home_dir, state,
            run_id=ctx.run_id or ctx.request_id, owner_id=getattr(agent.home_paths, 'owner_id', ''),
            thread_id=attrs.get('conversation_thread_id', ''), user_prompt=ctx.user_prompt,
            member_of=attrs.get('member_of', []), records=ctx.archive_tool_calls or ())
        if request and (request.get('targets') or request.get('executions')):
            ctx.tool_runtime_evidence['asset_evolution_run_id'] = request['run_id']
    except Exception:
        return


# LLM: 前台保留原 best-effort 学习入口；后台只交已绑定的宿主完成 DTO 给 W6 复核，未知真实错误不吞。
# 函数用途: 不改变回复保存开关或同步调用学习模型，按原来源登记方法学习请求。
def _request_skill_learning(agent: object, ctx: FinalizeContext) -> None:
    if getattr(ctx, "source", "") == "background_main_agent":
        from ..conversation.business_contracts import TrustedTaskCompletion

        evidence = ctx.tool_runtime_evidence if isinstance(ctx.tool_runtime_evidence, dict) else {}
        completion = evidence.get("trusted_task_completion")
        # 候选、聊天 JSON 和模型声称完成都不是 canonical 完成证明。W2 仍负责晚于收尾的持久确认。
        if not isinstance(completion, TrustedTaskCompletion) or not _conversation_turn_is_terminal(ctx):
            return
        attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
        if (ctx.context_scope == "task_local"
                or completion.owner_id != getattr(getattr(agent, "home_paths", None), "owner_id", None)
                or completion.thread_id != attrs.get("conversation_thread_id")
                or completion.run_id != ctx.run_id or completion.attempt_id != ctx.attempt_id
                or completion.source_kind != ctx.source or completion.tool_rounds != ctx.tool_rounds):
            return
        # 冻结端口由 W6 实现；消费端必须重新核验原 claim/completion 和准确业务任务绑定。
        from ..capability.task_learning_bridge import enqueue_completed_task_learning

        evidence["task_learning_enqueue"] = enqueue_completed_task_learning(completion=completion, host=agent)
        return
    enqueue = getattr(getattr(agent, "skill_learning", None), "enqueue_from_finalize", None)
    if not callable(enqueue):
        return
    try:
        enqueue(ctx)
    except Exception:
        return


def _finalization_learning_evidence(agent: object, ctx: FinalizeContext) -> dict[str, object]:
    """交给原运行结果的有界候选，不新增任务账、不签发完成、不调用学习模型。"""
    evidence = dict(ctx.tool_runtime_evidence or {})
    # host-only 运输对象不进入结果 JSON；W2 从 canonical 终态/claim 重建，不从结果字典恢复授权。
    evidence.pop("trusted_task_completion", None)
    if ctx.source != "background_main_agent" or ctx.context_scope == "task_local" or not _conversation_turn_is_terminal(ctx):
        return evidence
    import json
    from hashlib import sha256

    from ..capability.skill_learning_request import (
        TEXT_LIMIT_CHARS,
        TRACE_COUNT,
        _sample_learning_trace,
        _trace_entry,
        bounded_text,
    )
    from ..common.log_redaction import redact_sensitive_value
    from ..conversation.business_contracts import BusinessExecutionContext

    attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
    owner = getattr(getattr(agent, "home_paths", None), "owner_id", None)
    thread = attrs.get("conversation_thread_id")
    if not all(isinstance(value, str) and value for value in (owner, thread, ctx.task_id, ctx.run_id, ctx.attempt_id)):
        return evidence
    records = [row for row in ctx.archive_tool_calls or () if isinstance(row, dict)]
    trace = [_trace_entry(row) for row in _sample_learning_trace(records, limit=TRACE_COUNT)]
    business = attrs.get("business_context")
    # 业务选择只是材料快照。确切 session_task_ref→TaskRun、空间和资产版本仍须 W2/W6 从原账核验。
    if isinstance(business, BusinessExecutionContext):
        business = business.to_dict()
    elif isinstance(business, dict):
        try:
            business = BusinessExecutionContext.from_dict(business).to_dict()
        except (TypeError, ValueError):
            business = None
    else:
        business = None
    evidence["task_learning_candidate"] = {
        "schema_version": "task-learning-candidate.v1", "status": "pending_completion",
        "owner_id": owner, "thread_id": thread, "runtime_task_id": ctx.task_id,
        "run_id": ctx.run_id, "attempt_id": ctx.attempt_id, "source_kind": ctx.source,
        "tool_rounds": ctx.tool_rounds, "business_context": business,
        "user_prompt": bounded_text(ctx.user_prompt, TEXT_LIMIT_CHARS),
        "final_response": bounded_text(ctx.final_response.text, TEXT_LIMIT_CHARS),
        "tool_trace": trace,
        "trajectory_digest": sha256(json.dumps(redact_sensitive_value(records), sort_keys=True, default=str).encode()).hexdigest(),
        "result_refs": list(dict.fromkeys(_artifact_refs(records) + _latest_archive_refs(records))),
    }
    return evidence


# LLM: 回合收口是 PTY 会话的第一道回收点（真机实锤：git grep 在 PTY 里起分页器等按键，挂 9 小时没人收）。
#   owner 地址必须与工具侧同源：工具拿的是写边界 canonical_owner_home_root（来自 home_paths，Full Access
#   也不为空），不是 effective_owner_scope_root（本机管理员 Full Access 时为空，用它会对不上而静默漏收）。
#   其余身份只取宿主结构化字段：会话线程、任务与本次 run；回收失败不影响收口结果（空闲兜底与显式停止
#   路径仍在兜底），但回执必须结构化可查。
# 函数用途: 回合/任务结束时按结构化归属收回该归属的 PTY 会话，返回回执；任何异常都只记在回执里。
def _reclaim_turn_pty_sessions(agent: object, ctx: FinalizeContext) -> dict:
    import logging

    from ..path_access_policy import effective_owner_scope_root
    from ..tooling.pty_sessions import reclaim_pty_sessions

    try:
        canonical = str(getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or "")
        owner_home = canonical or effective_owner_scope_root(agent, context_scope=ctx.context_scope)
        attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
        receipt = reclaim_pty_sessions(
            owner_home,
            thread_id=str(attrs.get("conversation_thread_id") or ""),
            root_task_id=str(attrs.get("conversation_task_id") or ctx.task_id or ""),
            run_id=str(ctx.run_id or ""),
        )
    except Exception as exc:
        receipt = {"scope": "", "status": "error", "session_ids": [], "error_type": type(exc).__name__}
    if receipt.get("session_ids") or receipt.get("status") == "error":
        logging.getLogger(__name__).info("回合收口收回 PTY 会话: %s", receipt)
    return receipt


def _current_model_call_summary(
    agent: object,
    ctx: FinalizeContext,
    run_request_id: str,
) -> dict[str, object]:
    from .model.call_runtime import model_call_summary

    return model_call_summary(
        agent,
        request_id=str(ctx.request_id or run_request_id or ""),
        run_id=str(ctx.run_id or ctx.task_id or ""),
    )


# LLM: AgentRunResult exposes the same additive counters as the canonical ledger;
# this mapper must not recalculate usage from response text or context pressure.
# 函数用途: 把模型调用总账整理成运行结果的稳定数字字段。
def _model_call_result_fields(model_calls: dict[str, object]) -> dict[str, object]:
    status_counts = (
        model_calls.get("status_counts")
        if isinstance(model_calls.get("status_counts"), dict)
        else {}
    )
    return {
        "logical_model_turn_count": int(
            model_calls.get("logical_model_turn_count") or 0
        ),
        "physical_model_attempt_count": int(
            model_calls.get("physical_model_attempt_count") or 0
        ),
        "model_retry_count": int(model_calls.get("model_retry_count") or 0),
        "provider_http_attempt_count": int(
            model_calls.get("provider_http_attempt_count") or 0
        ),
        "provider_http_retry_count": int(
            model_calls.get("provider_http_retry_count") or 0
        ),
        "model_call_status_counts": {
            str(key): int(value or 0) for key, value in status_counts.items()
        },
        "model_accounted_input_tokens": int(
            model_calls.get("accounted_input_tokens") or 0
        ),
        "model_output_tokens": int(model_calls.get("output_tokens") or 0),
        "model_total_tokens": int(model_calls.get("total_tokens") or 0),
        "model_cached_input_tokens": int(
            model_calls.get("cached_input_tokens") or 0
        ),
        "model_cache_creation_input_tokens": int(
            model_calls.get("cache_creation_input_tokens") or 0
        ),
        "model_provider_usage_call_count": int(
            model_calls.get("provider_usage_call_count") or 0
        ),
        "model_estimated_usage_call_count": int(
            model_calls.get("estimated_usage_call_count") or 0
        ),
        "model_usage_breakdown": (
            dict(model_calls.get("usage_breakdown"))
            if isinstance(model_calls.get("usage_breakdown"), dict)
            else None
        ),
    }


def _estimate_token_params(ctx: FinalizeContext, run_request_id: str) -> EstimateTokenParams:
    turn_id = run_request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"
    return EstimateTokenParams(
        user_prompt=ctx.user_prompt,
        runtime_injections=ctx.runtime_injections,
        memories=ctx.memories,
        final_response=ctx.final_response,
        archive_tool_calls=ctx.archive_tool_calls,
        run_request_id=run_request_id,
        turn_id=turn_id,
        final_prompt=ctx.final_prompt,
        context_scope=ctx.context_scope,
        task_attributes=ctx.task_attributes,
    )


def _active_context_tokens(input_tokens: int, output_tokens: int, prompt_tokens: int) -> int:
    return max(int(input_tokens), int(prompt_tokens)) + int(output_tokens)


def _snapshot_result_fields() -> dict:
    return {
        "recovery_snapshot_id": "",
        "recovery_snapshot_path": "",
        "recovery_snapshot_error": "",
        "recovery_snapshot_token_estimate": 0,
    }


def _latest_archive_refs(records: list[object]) -> list[str]:
    return [
        str(record.get("raw_archive_path") or "")
        for record in records[-20:]
        if isinstance(record, dict) and str(record.get("raw_archive_path") or "")
    ]


def _artifact_refs(records: list[object]) -> list[str]:
    keys = ("artifact_ref", "artifact_path", "output_artifact_ref", "raw_archive_path")
    refs: list[str] = []
    for record in records[-20:]:
        if not isinstance(record, dict):
            continue
        refs.extend(str(record.get(key) or "") for key in keys if str(record.get(key) or ""))
    return refs


# LLM: 只读本 run 的能力包核验账本（能力包 v2 块 3）；读不了时没有事实，不影响交付。局部导入避免收尾依赖能力包子系统初始化。
# 函数用途: 取本 run 的能力包宿主核验事实，供最终结果和 Gateway 提示使用。
def _pack_verification_facts(agent: object, ctx: FinalizeContext) -> dict[str, object] | None:
    from ..capability.pack_verification_report import run_pack_verification_facts

    try:
        return run_pack_verification_facts(agent, ctx)
    except (OSError, ValueError):
        return None


# LLM: Channel attachments come only from ready artifact-registry records for declared output targets.
# 函数用途: 从本轮结构化工具记录提取可发送产物，不遍历 output/，也不从模型正文猜路径。
def _structured_delivery_artifacts(ctx: FinalizeContext, *, agent=None) -> list[dict[str, object]]:
    roots, exact_paths = _declared_delivery_targets(ctx.task_attributes, getattr(ctx, "delivery_contract", None))
    if not roots and not exact_paths:
        return []
    selected: dict[tuple[str, str], list[dict[str, object]]] = {}
    run_id, task_id, attempt_id = (str(getattr(ctx, key, '') or '') for key in ('run_id', 'task_id', 'attempt_id'))
    owner = str(getattr(getattr(agent, 'home_paths', None), 'owner_id', '') or '')
    from .child_delivery_artifacts import completed_child_artifact_refs
    child_refs = completed_child_artifact_refs(agent, ctx)

    def diagnostic(code):
        attrs = ctx.task_attributes
        if isinstance(attrs, dict):
            rows = attrs.setdefault('child_delivery_diagnostics', [])
            if isinstance(rows, list) and len(rows) < 8:
                rows.append({'error_code': code, 'coverage': 'partial', 'stage': 'delivery_observation',
                    'effect_outcome': 'not_started', 'run_id': run_id,
                    'next_action': '沿原产物ID和版本核对当前登记；不要盲重写、采用未登记字节或用旧版补交。'})

    for record in ctx.archive_tool_calls or []:
        if not isinstance(record, dict):
            continue
        if attempt_id and record.get('attempt_id') and record['attempt_id'] != attempt_id:
            if record.get('artifact_registry_refs'):
                diagnostic('DELIVERY_ARTIFACT_ATTEMPT_MISMATCH')
            continue
        refs = record.get("artifact_registry_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            origin = tuple(str(ref.get(key) or '') for key in ('run_id', 'task_id', 'agent_id'))
            if (run_id and origin[0] != run_id or task_id and origin[1] != task_id
                    or owner and origin[2] not in ('', owner, run_id)):
                diagnostic('DELIVERY_ARTIFACT_IDENTITY_MISMATCH')
                continue
            text = str(ref.get('path') or '')
            if not text:
                continue
            path = Path(text).expanduser().resolve(strict=False)
            if path not in exact_paths and not any(_path_is_within(path, root) for root in roots):
                continue
            key = (str(ref.get('artifact_id') or ''), str(path))
            previous = selected.get(key)
            if previous and tuple(str(previous[0].get(field) or '') for field in ('run_id', 'task_id', 'agent_id')) != origin:
                diagnostic('DELIVERY_ARTIFACT_IDENTITY_MISMATCH')
                continue
            selected.setdefault(key, []).append(ref)

    if selected and agent is not None:
        from ..artifacts.registry import latest_artifact_records_report
        from .run_task_workspace_writer import current_run_artifact_registry_root

        try:
            # 仅当前运行的一本原登记簿；超限明确partial，不扫描Owner或产物目录。
            report = latest_artifact_records_report(current_run_artifact_registry_root(agent, ctx), max_bytes=4 * 1024 * 1024)
            if report.errors:
                diagnostic('DELIVERY_ARTIFACT_REGISTRY_UNREADABLE_OR_PARTIAL')
                selected.clear()
            else:
                for key, candidates in list(selected.items()):
                    canonical = report.records.get(key[0])
                    # 原登记物理顺序/当代canonical才是版本权威；不按墙钟、归档晚到或旧摘要决定。
                    matched = next((ref for ref in reversed(candidates) if canonical is not None and all(
                        getattr(canonical, field) == ref.get(field) for field in
                        ('artifact_id', 'run_id', 'task_id', 'agent_id', 'path', 'sha256', 'size_bytes', 'status'))), None)
                    if matched is None:
                        diagnostic('DELIVERY_ARTIFACT_VERSION_MISMATCH')
                        selected.pop(key)
                    else:
                        selected[key] = [matched]
        except (OSError, ValueError, TypeError, RuntimeError):
            diagnostic('DELIVERY_ARTIFACT_REGISTRY_UNAVAILABLE')
            selected.clear()
    result = [item for candidates in selected.values()
              if (item := _delivery_artifact_from_registry_ref(candidates[-1], roots, exact_paths)) is not None]
    # 子代仍只沿既有精确父谱系核验通道，不拿父登记簿或同名路径收养其它执行体。
    seen = {(item['artifact_id'], item['path']) for item in result}
    for ref in child_refs:
        item = _delivery_artifact_from_registry_ref(ref, roots, exact_paths)
        if item is not None and (item['artifact_id'], item['path']) not in seen:
            result.append(item)
            seen.add((item['artifact_id'], item['path']))
    return result


# LLM: 实际Agent输出须复读原canonical操作与本ctx；不从模型/MCP字典或正文认领回复槽，纯投影调用不授执行权。
# 函数用途: 提取同轮成功直发事实，按receipt去重并验证真实owner、尝试及来源绑定。
def _message_tool_deliveries(ctx: FinalizeContext, *, agent: object = None) -> list[dict[str, object]]:
    deliveries: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in list(ctx.archive_tool_calls or []):
        if not isinstance(record, dict) or record.get("ok") is not True:
            continue
        if str(record.get("tool") or "") != "send_message":
            continue
        envelope = record.get("tool_result_envelope")
        envelope = envelope if isinstance(envelope, dict) else {}
        evidence = envelope.get("delivery_evidence")
        if not isinstance(evidence, dict):
            continue
        if (
            str(evidence.get("delivery_status") or "").strip().lower() != "sent"
            or evidence.get("source_owner_delivery") is not True
        ):
            continue
        receipt_id = str(evidence.get("receipt_id") or "").strip()
        if not receipt_id or receipt_id in seen:
            continue
        binding = _verified_reply_binding(agent, ctx, record, evidence)
        if not binding:
            continue
        seen.add(receipt_id)
        deliveries.append({**evidence, 'reply_binding': binding})
    return deliveries


# LLM: 原工具账和本次归档必须同时成功且精确同attempt；任意模型/MCP字典或旧回执不能占当前owner回复槽。
# 函数用途: 从真实send_message操作复读已提交身份，向后台只投影当前发送的规范回执。
def _verified_reply_binding(agent, ctx, record, evidence):
    from ..capability.channel_message_tool import SendMessageTool
    from ..local_storage.tool_operations import ToolOperationStateError
    from ..runtime_db.managed_operation_store import ManagedOperationStore

    binding = evidence.get('reply_binding')
    if not isinstance(binding, dict) or agent is None:
        return {}
    if not isinstance(getattr(getattr(agent, 'tools', None), 'tools', {}).get('send_message'), SendMessageTool):
        return {}
    scope = record.get('run_scope', {})
    keys = ('request_id', 'run_id', 'task_id', 'attempt_id')
    if (record.get('handler_executed') is not True or record.get('tool_operation_status') != 'succeeded'
            or any(binding.get(key) != getattr(ctx, key, None) or binding.get(key) != scope.get(key) for key in keys)
            or binding.get('thread_id') != scope.get('session_id')
            or binding.get('owner_id') != agent.home_paths.owner_id
            or binding.get('owner_home') != str(agent.home_paths.owner_home_dir)):
        return {}
    operation_id = str(record.get('operation_id') or '')
    if not operation_id:
        return {}
    try:
        operation = ManagedOperationStore(agent.subagents.runtime_db).get_tool_operation(
            owner_id=binding['owner_id'], run_id=binding['run_id'], task_id=binding['task_id'],
            attempt_id=binding['attempt_id'], operation_id=operation_id, tool_name='send_message')
    except ToolOperationStateError:
        return {}
    if (operation is None or operation.status != 'succeeded' or operation.unknown_reason
            or operation.result.get('ok') is not True or operation.result.get('handler_executed') is not True
            or operation.result.get('result_envelope', {}).get('delivery_evidence') != evidence):
        return {}
    return {**binding, 'operation_id': operation_id}


def _declared_delivery_targets(task_attributes: object, contract: object = None) -> tuple[tuple[Path, ...], frozenset[Path]]:
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    workspace = attrs.get("run_workspace") if isinstance(attrs.get("run_workspace"), dict) else {}
    roots: list[Path] = []
    for key in ("output_dir", "user_requested_output_dir"):
        text = str(workspace.get(key) or "").strip()
        if text:
            roots.append(Path(text).expanduser().resolve(strict=False))
    exact: set[Path] = set()
    if isinstance(contract, dict):
        delivery_workspace = contract.get("task_workspace")
        output = delivery_workspace.get("output_dir") if isinstance(delivery_workspace, dict) else None
        if isinstance(output, str) and output.strip():
            roots.append(Path(output.strip()).expanduser().resolve(strict=False))
        artifacts = contract.get("artifacts")
        for artifact in artifacts if isinstance(artifacts, (list, tuple)) else ():
            if isinstance(artifact, dict):
                allowed = artifact.get("allowed_output_roots")
                for root in allowed if isinstance(allowed, (list, tuple)) else ():
                    if isinstance(root, str) and root.strip():
                        roots.append(Path(root).expanduser().resolve(strict=False))
                for key in ("path", "preferred_path", "target_path", "output_path"):
                    path = artifact.get(key)
                    if isinstance(path, str) and path.strip():
                        exact.add(Path(path).expanduser().resolve(strict=False))
    text = str(workspace.get("user_requested_output_path") or "").strip()
    if text:
        exact.add(Path(text).expanduser().resolve(strict=False))
    return tuple(dict.fromkeys(roots)), frozenset(exact)


def _delivery_artifact_from_registry_ref(
    ref: object,
    roots: tuple[Path, ...],
    exact_paths: frozenset[Path],
) -> dict[str, object] | None:
    if not isinstance(ref, dict) or str(ref.get("status") or "").strip().lower() != "ready":
        return None
    text = str(ref.get("path") or "").strip()
    if not text:
        return None
    path = Path(text).expanduser().resolve(strict=False)
    if path not in exact_paths and not any(_path_is_within(path, root) for root in roots):
        return None
    return {
        "artifact_id": str(ref.get("artifact_id") or ""),
        "path": str(path),
        "name": path.name,
        "kind": str(ref.get("kind") or "file"),
        "sha256": str(ref.get("sha256") or ""),
        "size_bytes": _nonnegative_int(ref.get("size_bytes")),
        **{key: ref[key] for key in ('task_id', 'run_id')
           if isinstance(ref.get(key), str) and ref[key].strip()},
        "ok": True,
    }


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _resume_context_fields(ctx: FinalizeContext) -> dict:
    resume = ctx.resume_context_result
    return {
        "memory_resume_context_injected": resume.injected if resume else False,
        "memory_resume_context_query": resume.query if resume else "",
        "memory_resume_context_matches": (
            resume.archive_match_count + resume.local_match_count + resume.task_fact_source_count
        )
        if resume
        else 0,
        "memory_resume_context_token_estimate": estimate_tokens(resume.context_block)
        if resume and resume.injected
        else 0,
        "memory_resume_context_error": resume.error if resume else "",
    }


# LLM: Completion authority is the host-owned turn_end_reason, never a phrase in model text.
# 函数用途: 判断本轮是否为可关闭普通会话任务的正常最终响应；等待、后台交接和失败状态保持任务活跃。
def _conversation_turn_is_terminal(ctx: FinalizeContext) -> bool:
    response = ctx.final_response
    turn_end_reason = infer_turn_end_reason(
        explicit=getattr(response, "turn_end_reason", ""),
        runtime_status=getattr(response, "runtime_status", "ok"),
        runtime_reason=getattr(response, "runtime_reason", ""),
        stop_reason=getattr(response, "stop_reason", ""),
    )
    if turn_end_reason != TurnEndReason.COMPLETED.value:
        return False
    reason = str(getattr(response, "runtime_reason", "") or "").strip().lower()
    return reason not in {
        "background_dispatch",
        "repeated_tool_failure",
        "repeated_tool_failure_exhausted",
    }


# LLM: 持久目标与 turn 完成分离；续跑按 turn_end 的结构化判据，工具数和 Todo 不能决定生命周期。
# 函数用途: 前台正常回复或可恢复技术收口后保持目标运行；后台由 wake 消费回执接续，避免双重调度。
def _schedule_typed_unfinished_continuation(agent: object, ctx: FinalizeContext) -> None:
    """Resume an explicit persistent Goal after a structured turn boundary."""
    source = str(ctx.source or "").strip().lower()
    # 后台先完成当前 wake 的回执，再由 scheduler 发布下一条；提前发布会被当前 pending wake 吞掉。
    if source == "background_main_agent" or not ctx.do_save:
        return
    if str(ctx.context_scope or "").strip().lower() == "task_local":
        return
    attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
    # A named Audit owns durable, lease-backed source workers. Host-level
    # reconciliation keeps those workers alive and structured lifecycle events
    # wake the coordinator only when its judgment is needed.
    if str(attrs.get("conversation_work_kind") or "").strip().lower() == "audit":
        return
    goal_id = str(attrs.get("thread_goal_id") or "").strip()
    if not goal_id:
        # 用户可在运行中通过控制面建目标；这里只查同 task 的记录，普通任务不自动建 Goal。
        store = getattr(agent, "conversation_store", None)
        thread_id = str(attrs.get("conversation_thread_id") or "")
        task_id = str(attrs.get("conversation_task_id") or "")
        if store is None or not thread_id or not task_id:
            return
        goal = store.goals.load(thread_id, task_id=task_id)
        if goal is None or goal.task_id != task_id or goal.status != "active":
            return
        goal_id = goal.goal_id

    # Goal mode uses the shared typed continuation gate. Blocked, cancelled,
    # protocol-violating and unknown-effect turns never acquire a future tick.
    from ..turn_end import should_continue_task

    should, _reason = should_continue_task(ctx.final_response)
    if not should and not _conversation_turn_is_terminal(ctx):
        return
    from ..conversation.runtime import ensure_goal_progress_continuation
    thread_id = str(attrs.get("conversation_thread_id") or "")
    ensure_goal_progress_continuation(
        agent,
        task_id=str(attrs.get("conversation_task_id") or ""),
        thread_id=thread_id,
        goal_id=goal_id,
    )
