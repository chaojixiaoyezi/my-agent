from __future__ import annotations

import hashlib
import time as time_module
from dataclasses import dataclass
from pathlib import Path

from ..conversation.task_state import conversation_task_completed
from ..memory_archive import (
    archive_run_turn,
    estimate_tokens,
)
from ..memory_archive.runtime.turn_archiver import ArchiveRunTurnParams, ArchiveTurnContext
from ..memory_archive.runtime_fact_source import RuntimeFactSourceRequest, write_runtime_fact_source
from ..memory_archive.tokens import TurnTokenUsage, append_session_token_usage
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
from .runtime.owner_roots import runtime_archive_roots


@dataclass(frozen=True)
class BuildAgentRunResultParams:
    ctx: FinalizeContext
    archive_result: object
    token_ledger: dict[str, int]
    run_request_id: str
    model_calls: dict[str, object]


class FinalizationService:
    def __init__(self, agent):
        self._agent = agent

    # LLM: Finalization archives run facts and only requests Curator after a typed task terminal state; it never writes dialogue to long-term.
    # 函数用途: 收口一轮模型运行、写恢复事实并在任务完成时登记后台记忆提炼。
    def finalize(self, ctx: FinalizeContext):
        assert ctx.final_response is not None
        _mark_open_goal_progress_unfinished(self._agent, ctx)
        if _conversation_turn_is_terminal(ctx):
            # 会话运行时 的普通 turn 以运行时最终响应事件结束；不解析“做完了”等自然语言，
            # 也不再扫描 output/ 或要求模型额外提交验收。
            from ..conversation.task_promotion import complete_current_conversation_task

            complete_current_conversation_task(
                self._agent,
                ctx.task_attributes,
                source=ctx.source,
                current_task_id=ctx.task_id,
            )
        run_request_id = ctx.request_id or f"run-{time_module.time_ns()}"

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
        self._write_thread_model_usage_if_bound(ctx, run_request_id, model_calls)
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
        _schedule_typed_unfinished_continuation(self._agent, ctx)
        from ..conversation.goal_runtime import schedule_goal_activated_in_turn
        from .runtime.goal_accounting import finish_goal_turn_accounting

        schedule_goal_activated_in_turn(self._agent, ctx.task_attributes)
        finish_goal_turn_accounting(self._agent, ctx.task_attributes)
        if conversation_task_completed(ctx.task_attributes):
            _request_memory_curator(self._agent, "task_complete")
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
                    response_text=ctx.final_response.text,
                    backend=ctx.final_response.backend,
                    status=str(getattr(ctx.final_response, "runtime_status", "ok") or "ok"),
                    next_actions=ctx.recovery_next_actions or [],
                    archive_tool_calls=ctx.archive_tool_calls or [],
                    runtime_injections=tuple(str(item) for item in ctx.runtime_injections or []),
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

    # LLM: Provider usage follows the exact conversation/agent thread even when
    # do_save=False. Each finalization submits the cumulative request/run
    # snapshot at its physical-attempt cursor; ConversationStore atomically
    # persists only the new delta so Compact retries neither collide nor double
    # count. This stays independent from transcript, memory and run archives.
    # 函数用途: 将本轮累计模型账按物理调用游标幂等写入所属会话；没有可信线程身份时明确跳过而不猜。
    def _write_thread_model_usage_if_bound(
        self,
        ctx: FinalizeContext,
        run_request_id: str,
        model_calls: dict[str, object],
    ) -> str:
        if int(model_calls.get("physical_model_attempt_count") or 0) <= 0:
            return ""
        attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
        thread_id = str(
            attrs.get("agent_thread_id")
            or attrs.get("conversation_thread_id")
            or ""
        ).strip()
        store = getattr(self._agent, "conversation_store", None)
        append_usage = getattr(store, "append_model_usage_snapshot_once", None)
        if not thread_id or not callable(append_usage):
            return ""
        request_id = str(ctx.request_id or run_request_id or "").strip()
        physical_attempt_count = int(
            model_calls.get("physical_model_attempt_count") or 0
        )
        identity = "\x1f".join(
            (
                thread_id,
                request_id,
                str(ctx.run_id or "").strip(),
                str(ctx.task_id or "").strip(),
                str(ctx.source or "").strip(),
                str(physical_attempt_count),
            )
        )
        event_id = "usage-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        event = append_usage(
            {
                "event_id": event_id,
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

    # LLM: Run archive/task workspace preserve experience refs only; ordinary user/assistant bodies never enter formal long-term here.
    # 函数用途: 按保存开关写任务工作区和运行经历档案，不做长期事实晋升。
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
                        run_id=params.run_id,
                        task_id=params.task_id,
                        user_prompt=params.user_prompt,
                        response_text=params.final_response.text,
                        backend=params.final_response.backend,
                        tool_calls=params.archive_tool_calls or [],
                        source=params.source,
                        archive_level=int(getattr(self._agent.config, "memory_archive_level", 3)),
                        preview_limits=_memory_archive_preview_limits(self._agent.config),
                        summary_chars=int(
                            getattr(self._agent.config, "memory_archive_summary_chars", 96) or 96
                        ),
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
                    + estimate_tokens(params.runtime_injections)
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

    def _build_agent_run_result(self, params: BuildAgentRunResultParams):
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
        return AgentRunResult(
            prompt=ctx.final_prompt,
            response=redact_executed_operation_labels(
                honest_text,
                operation_verification,
            ),
            backend=ctx.final_response.backend,
            used_memories=len(ctx.memories),
            model_response=honest_text,
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
            runtime_injection_token_estimate=estimate_tokens(ctx.runtime_injections)
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
            delivery_artifacts=_structured_delivery_artifacts(ctx),
            message_tool_deliveries=_message_tool_deliveries(ctx),
            operation_verification=operation_verification,
            tool_runtime_evidence=dict(ctx.tool_runtime_evidence or {}),
            active_turn_user_inputs=list(ctx.active_turn_user_inputs or []),
            **compact_auto_cycle_fields(
                self._agent, ctx, params.token_ledger, request_id=params.run_request_id
            ),
        )


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


# LLM: Channel attachments come only from ready artifact-registry records for declared output targets.
# 函数用途: 从本轮结构化工具记录提取可发送产物，不遍历 output/，也不从模型正文猜路径。
def _structured_delivery_artifacts(ctx: FinalizeContext) -> list[dict[str, object]]:
    roots, exact_paths = _declared_delivery_targets(ctx.task_attributes)
    if not roots and not exact_paths:
        return []
    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for record in list(ctx.archive_tool_calls or []):
        if not isinstance(record, dict):
            continue
        refs = record.get("artifact_registry_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            item = _delivery_artifact_from_registry_ref(ref, roots, exact_paths)
            if item is None:
                continue
            key = (str(item.get("artifact_id") or ""), str(item.get("path") or ""))
            if key in seen:
                continue
            seen.add(key)
            selected.append(item)
    return selected


# LLM: send_message 已送达事实只认工具结果 envelope；不要从模型正文、工具名出现次数或日志猜。
# 函数用途: 从本轮 archive 中提取并按 receipt 去重的当前 owner 消息交付证据。
def _message_tool_deliveries(ctx: FinalizeContext) -> list[dict[str, object]]:
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
        seen.add(receipt_id)
        deliveries.append(dict(evidence))
    return deliveries


def _declared_delivery_targets(task_attributes: object) -> tuple[tuple[Path, ...], frozenset[Path]]:
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    workspace = attrs.get("run_workspace") if isinstance(attrs.get("run_workspace"), dict) else {}
    roots: list[Path] = []
    for key in ("output_dir", "user_requested_output_dir"):
        text = str(workspace.get(key) or "").strip()
        if text:
            roots.append(Path(text).expanduser().resolve(strict=False))
    exact: set[Path] = set()
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


def _memory_archive_preview_limits(config) -> dict[int, int]:
    return {
        0: int(getattr(config, "memory_archive_preview_level_0_chars", 2048) or 0),
        1: int(getattr(config, "memory_archive_preview_level_1_chars", 1024) or 0),
        2: int(getattr(config, "memory_archive_preview_level_2_chars", 512) or 0),
        3: int(getattr(config, "memory_archive_preview_level_3_chars", 160) or 0),
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


# LLM: Root conversation turns may rent the owner scheduler, while task-local
# child turns must remain on their own runner/thread lane. Never turn a child
# checkpoint into a background-main turn with root tools.
# 函数用途: 为未完成的主会话安排续跑；子代理只保存断点并交回自己的 runner 续派。
def _schedule_typed_unfinished_continuation(agent: object, ctx: FinalizeContext) -> None:
    """Resume explicit persistent root work after a structured turn boundary."""
    if not ctx.do_save:
        return
    if str(ctx.context_scope or "").strip().lower() == "task_local":
        return
    attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
    # A named Audit owns durable, lease-backed source workers.  Host-level
    # reconciliation keeps those workers alive and structured finding/lifecycle
    # events wake the coordinator only when its judgment is needed.  Scheduling
    # an ordinary model turn here would poll the coordinator while sources are
    # healthy and duplicate that runtime.
    if str(attrs.get("conversation_work_kind") or "").strip().lower() == "audit":
        return
    reason = str(getattr(ctx.final_response, "runtime_reason", "") or "").strip().upper()
    # 2026-08-14 根因3 设计 v2(审查意见2): gate 判断提取为共享函数
    # should_continue_task(conversation/runtime.py)——gateway 调度器与 CLI
    # resume_loop 用同一判断, 杜绝两份逻辑漂移。逻辑与旧内联 gate 完全等价:
    # 返工门 unfinished 族 + {TASK_PROGRESS_OPEN, TOOL_ROUND_LIMIT_REACHED,
    # REPEATED_TOOL_FAILURE}; blocked/协议违规/UNKNOWN 不续跑。
    from ..conversation.runtime import should_continue_task

    should, _reason = should_continue_task(ctx.final_response)
    if not should:
        return
    from ..conversation.runtime import (
        ensure_goal_progress_continuation,
        ensure_ordinary_task_resume,
    )
    from .runtime.task_identity import durable_task_id

    thread_id = str(attrs.get("conversation_thread_id") or "")
    # 前台会话任务立即 expedite 续跑:gateway worker 硬编码 source="gateway",
    # 直跑传 chat/cli_run,HTTP 适配器传 http:<channel>,cli_* 是预留前缀;
    # 排除法比旧白名单 {gateway,chat,cli_run} 更健壮(任何前台形态都不漏)。
    # 后台唤醒轮/子代理收口按 policy interval 自然触发,不抢占资源。
    source = str(ctx.source or "").strip().lower()
    foreground = (
        source.startswith("cli_")
        or source.startswith("http:")
        or source in {"gateway", "chat", "cli_run"}
        or not source
    )
    if str(attrs.get("thread_goal_id") or "").strip():
        ensure_goal_progress_continuation(
            agent,
            task_id=str(durable_task_id(ctx) or attrs.get("root_task_id") or ""),
            thread_id=thread_id,
            due_now=foreground,
        )
        return
    # 普通任务(无 /goal):只有轮限/失败等宿主结构化 unfinished
    # 状态才进入持久恢复链；task_progress 账本不参与。后台唤醒轮本身是
    # 「读状态、给回执」语义，不能再排一个相同唤醒。
    if source == "background_main_agent":
        return
    from .runtime.task_identity import progress_ledger_id

    ensure_ordinary_task_resume(
        agent,
        task_id=str(progress_ledger_id(agent, ctx) or attrs.get("root_task_id") or ""),
        thread_id=thread_id,
        due_now=foreground,
    )


# LLM: only an explicit persistent goal owns an open-plan lifecycle gate; ordinary task_progress
# remains advisory and must not alter the current turn or any later turn.
# 函数用途: 仅在 `/goal` 的计划还没完成时保持任务运行，普通任务不会被旧清单卡住。
def _mark_open_goal_progress_unfinished(agent: object, ctx: FinalizeContext) -> None:
    """Keep the explicit persistent ``/goal`` lifecycle open with open plan items.

    Ordinary tasks may keep a progress note for memory and compact recovery,
    but that note neither gates completion nor schedules future execution.
    """

    attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
    if (
        not ctx.do_save
        or conversation_task_completed(ctx.task_attributes)
        or not str(attrs.get("thread_goal_id") or "").strip()
        or str(getattr(ctx.final_response, "runtime_status", "ok") or "ok").strip().lower() != "ok"
    ):
        return
    from .runtime.task_identity import progress_ledger_id

    # 读侧必须与写侧同一把 key:task_progress_tool 用 progress_ledger_id 记账
    # (会话任务=task-path:<目录指纹>),收口用 durable_task_id(原始 id)读=读错位
    # =恒 0=有 open item 也误收口(问题4:任务没做完 link 就转终态)。
    task_id = str(progress_ledger_id(agent, ctx) or attrs.get("root_task_id") or "").strip()
    if not task_id:
        return
    from ..conversation.runtime import ledger_open_progress_item_count

    if ledger_open_progress_item_count(agent, task_id) <= 0:
        return
    ctx.final_response.runtime_status = "unfinished"
    ctx.final_response.runtime_reason = "TASK_PROGRESS_OPEN"
    ctx.final_response.runtime_source = "task_progress"
