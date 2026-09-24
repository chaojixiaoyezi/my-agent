# LLM: This module bridges a provider-overflowing carried active turn into the existing canonical
# Conversation Compact ledger. Full archives remain runtime authority; only committed exact call
# refs decide which records are replaced. Missing origin remains visible with structured uncertainty.
# 模块用途: 将跨进程续跑的长工具历史正式记成 Compact，并在恢复时只向模型展示摘要与近期调用。

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..memory_archive import estimate_tokens
from ..tooling.call_ref import ToolCallRef, tool_call_ref_from_record
from .authority import (
    AGENT_THREAD_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from .compact_checkpoint import committed_live_tool_compact_sources
from .compact_guard import CompactInterruptCheck, raise_if_compact_interrupted
from .compact_progress import (
    COMPACT_AUTHORITY_CONVERSATION,
    COMPACT_SOURCE_ACTIVE_TURN,
    CONVERSATION_COMPACT_PROGRESS_SCHEMA,
)
from .live_tool_compact import (
    LiveToolCompactCommitRequest,
    commit_live_tool_compact,
)
from .models import ConversationThread


# LLM: The result separates canonical generation movement from the unchanged full archive records.
# 类用途: 返回是否提交、替代与保留边界及来源不确定计数；未知来源不会借当前身份提交。
@dataclass(frozen=True)
class ActiveTurnArchiveCompactResult:
    thread: ConversationThread
    compacted: bool = False
    source_call_ids: tuple[str, ...] = ()
    retained_call_ids: tuple[str, ...] = ()
    projected_tokens_before: int = 0
    projected_tokens_after: int = 0
    uncertain_call_count: int = 0
    source_resolution: str = "not_evaluated"


# LLM: The caller supplies one immutable overflow identity, UI projection and typed interrupt
# check; the request contains no task status or completion authority and cannot change binding.
# 类用途: 打包跨工作片压缩所需的请求、尝试、原任务、进度回调和当前停止检查。
@dataclass(frozen=True)
class ActiveTurnArchiveCompactRequest:
    task_attributes: object
    request_id: str
    attempt_id: str
    task_prompt: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = None
    interrupt_check: CompactInterruptCheck | None = None


# LLM: This candidate freezes one exact checkpoint boundary before the summary model call. The
# full archive is intentionally absent because only the model-visible replacement belongs here.
# 类用途: 保存一次待提交压缩的线程、策略、替代区、保留区和进度计量。
@dataclass(frozen=True)
class _ActiveTurnArchiveCompactPlan:
    thread: ConversationThread
    binding: object
    policy: object
    visible_records: tuple[dict[str, object], ...]
    source_call_ids: tuple[str, ...]
    retained_records: tuple[dict[str, object], ...]
    retained_call_ids: tuple[str, ...]
    source_call_refs: tuple[ToolCallRef, ...]
    retained_call_refs: tuple[ToolCallRef | None, ...]
    projected_tokens_before: int
    progress: dict[str, object]


# LLM: Runtime dedupe/audit continues to receive every record. Only the model projection excludes
# exact calls named by the committed checkpoint chain; unreadable authority fails closed. Legacy
# bare ids and records without canonical origin remain visible and explicitly uncertain.
# 函数用途: 从完整本轮工具账中筛出尚未被 Compact 摘要替代、仍需直接展示给模型的记录。
def model_visible_active_turn_tool_calls(
    agent: object,
    task_attributes: object,
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    values = [dict(item) for item in records if isinstance(item, dict)]
    if not values or not _transcript_authoritative(task_attributes):
        return values
    thread = _load_authoritative_thread(agent, task_attributes)
    sources = committed_live_tool_compact_sources(agent, thread)
    visible: list[dict[str, object]] = []
    for item in values:
        ref = tool_call_ref_from_record(item)
        if ref is not None and ref in sources.refs:
            continue
        reasons = []
        if ref is None:
            reasons.append("missing_tool_call_ref")
        if _record_call_id(item) in sources.uncertain_call_ids:
            reasons.append("checkpoint_source_unknown")
        if reasons:
            item["compact_source_resolution"] = {
                "status": "uncertain", "reasons": reasons,
                "checkpoint_ids": list(sources.uncertain_checkpoint_ids),
            }
        visible.append(item)
    return visible


# LLM: This is the recovery counterpart of the in-process native IR compactor. It runs only after
# a real context_overflow and a transcript Compact no-op, then reuses the same checkpoint/CAS ledger
# instead of treating a fresh Agent.run as an implicit uncounted compaction.
# 函数用途: 把已溢出的跨工作片工具历史压成正式摘要，保留完整账本与近期调用后推进一次 Compact。
def compact_carried_active_turn_archive(
    agent: object,
    store: object,
    thread: ConversationThread,
    records: list[dict[str, object]],
    request: ActiveTurnArchiveCompactRequest,
) -> ActiveTurnArchiveCompactResult:
    if not _transcript_authoritative(request.task_attributes):
        return ActiveTurnArchiveCompactResult(thread=thread)
    visible = model_visible_active_turn_tool_calls(
        agent,
        request.task_attributes,
        records,
    )
    indexed = [(item, _record_call_id(item)) for item in visible]
    if not indexed:
        return ActiveTurnArchiveCompactResult(thread=thread)
    uncertain_count = sum(tool_call_ref_from_record(item) is None for item, _call_id in indexed)
    if uncertain_count == len(indexed):
        return ActiveTurnArchiveCompactResult(
            thread=thread, uncertain_call_count=uncertain_count, source_resolution="uncertain",
        )

    from ..agent_core.runtime.context_compactor import runtime_compact_policy
    from .live_tool_compact import resolve_live_tool_compact_binding

    policy = runtime_compact_policy(
        agent,
        save=False,
        task_attributes=request.task_attributes,
    )
    binding = resolve_live_tool_compact_binding(
        agent,
        task_attributes=request.task_attributes,
        policy=policy,
    )
    if binding is None:
        return ActiveTurnArchiveCompactResult(thread=thread)
    if binding.thread.thread_id != thread.thread_id or binding.store is not store:
        raise OSError("active-turn compact binding does not match the caller thread")
    plan = _build_active_turn_compact_plan(binding, policy, visible, indexed)
    return _execute_active_turn_compact(agent, plan, request)


# LLM: Boundaries are selected from whole records before any model call. The newest complete tail is
# retained and at least one known call remains in the source. Unknown-origin records always stay
# visible; source/tail selection uses positions and exact refs, never a bare-id set.
# 函数用途: 按统一近期 token 预算冻结一代 active-turn Compact 的移除区和保留区。
def _build_active_turn_compact_plan(
    binding: object,
    policy: object,
    visible: list[dict[str, object]],
    indexed: list[tuple[dict[str, object], str]],
) -> _ActiveTurnArchiveCompactPlan:
    retained = _recent_records_within_budget(indexed, policy.recent_tail_tokens)
    tail_start = len(indexed) - len(retained)
    refs = [tool_call_ref_from_record(item) for item, _call_id in indexed]
    source_positions = {index for index, ref in enumerate(refs) if index < tail_start and ref is not None}
    if not source_positions:
        # 溢出时至少替换一条已知来源；未知记录不能因位置靠前被强行归入当前身份。
        source_positions = {next(index for index, ref in enumerate(refs) if ref is not None)}
    source_ids = tuple(call_id for index, (_item, call_id) in enumerate(indexed) if index in source_positions)
    retained = [item for index, item in enumerate(indexed) if index not in source_positions]
    retained_ids = tuple(call_id for _item, call_id in retained)
    thread = binding.thread
    generation = max(0, int(thread.compact_generation or 0)) + 1
    before_tokens = estimate_tokens({"active_turn_tool_calls": visible})
    return _ActiveTurnArchiveCompactPlan(
        thread=thread,
        binding=binding,
        policy=policy,
        visible_records=tuple(visible),
        source_call_ids=source_ids,
        retained_records=tuple(item for item, _call_id in retained),
        retained_call_ids=retained_ids,
        source_call_refs=tuple(ref for index, ref in enumerate(refs) if index in source_positions),
        retained_call_refs=tuple(ref for index, ref in enumerate(refs) if index not in source_positions),
        projected_tokens_before=before_tokens,
        progress={
            "generation": generation,
            "operation_id": f"active-turn-archive:{uuid.uuid4().hex}",
            "source_kind": COMPACT_SOURCE_ACTIVE_TURN,
            "commit_authority": COMPACT_AUTHORITY_CONVERSATION,
            "before_tokens": before_tokens,
            "trigger_tokens": max(0, int(policy.trigger_tokens or 0)),
            "context_window_tokens": max(0, int(policy.context_window_tokens or 0)),
            "source_messages": len(source_ids) * 2,
        },
    )


# LLM: Summary, checkpoint and CAS are one interruptible candidate. User stop discards it without
# failure accounting; only a real error records the shared circuit while the full archive stays.
# 函数用途: 可中断地生成并提交跨工作片摘要；停止时保留原工具账并收起进度块。
def _execute_active_turn_compact(
    agent: object,
    plan: _ActiveTurnArchiveCompactPlan,
    request: ActiveTurnArchiveCompactRequest,
) -> ActiveTurnArchiveCompactResult:
    from .compact_guard import compact_exception_code
    from .live_tool_compact import record_live_tool_compact_failure

    callback = request.progress_callback
    progress = plan.progress
    raise_if_compact_interrupted(request.interrupt_check)
    _emit_progress(callback, progress, phase="started", stage="preparing", percent=5)
    _emit_progress(callback, progress, phase="progress", stage="summarizing", percent=20)
    try:
        replacement = _active_turn_replacement_summary(agent, plan, request)
        raise_if_compact_interrupted(request.interrupt_check)
        after_tokens = estimate_tokens(
            {
                "compact_summary": replacement,
                "retained_active_turn_tool_calls": plan.retained_records,
            }
        )
        _emit_progress(
            callback,
            progress,
            phase="progress",
            stage="measuring",
            percent=65,
            after_tokens=after_tokens,
        )
        _emit_progress(
            callback,
            progress,
            phase="progress",
            stage="checkpointing",
            percent=82,
            after_tokens=after_tokens,
        )
        updated = _commit_active_turn_compact(agent, plan, request, replacement, after_tokens)
    except InterruptedError:
        _emit_progress(
            callback,
            progress,
            phase="superseded",
            stage="candidate_discarded",
            percent=0,
        )
        raise
    except Exception as exc:
        # A transport may surface its own exception after the stop callback closes it. Recheck
        # typed interruption before classifying that close as a Compact/provider failure.
        try:
            raise_if_compact_interrupted(request.interrupt_check)
        except InterruptedError:
            _emit_progress(
                callback,
                progress,
                phase="superseded",
                stage="candidate_discarded",
                percent=0,
            )
            raise
        record_live_tool_compact_failure(plan.binding, exc)
        _emit_progress(
            callback,
            progress,
            phase="failed",
            stage="failed",
            percent=0,
            error_code=compact_exception_code(exc),
        )
        raise
    _emit_progress(
        callback,
        {**progress, "generation": max(1, int(updated.compact_generation or 1))},
        phase="completed",
        stage="completed",
        percent=100,
        after_tokens=after_tokens,
    )
    return ActiveTurnArchiveCompactResult(
        thread=updated,
        compacted=True,
        source_call_ids=plan.source_call_ids,
        retained_call_ids=plan.retained_call_ids,
        projected_tokens_before=plan.projected_tokens_before,
        projected_tokens_after=after_tokens,
        uncertain_call_count=sum(ref is None for ref in plan.retained_call_refs),
        source_resolution="uncertain" if None in plan.retained_call_refs else "complete",
    )


# LLM: The one summary call merges the previous canonical summary with a bounded typed handoff and
# must return the validated live Compact replacement shape before any checkpoint is written.
# 函数用途: 生成可独立替代上一代摘要的六字段交接，不叠加无界摘要链。
def _active_turn_replacement_summary(
    agent: object,
    plan: _ActiveTurnArchiveCompactPlan,
    request: ActiveTurnArchiveCompactRequest,
) -> str:
    from ..agent_core.runtime.loop_support import reconstructed_model_tool_context
    from ..backends.tool_ir import RuntimeFactsTurn
    from ..memory_archive.compact_semantic_summary import (
        LiveToolHistorySummaryRequest,
        semantic_summary_config,
        summarize_live_tool_history,
    )
    from .tool_context_window import native_carried_tool_handoff

    config = semantic_summary_config(agent)
    if not config.enabled:
        raise ValueError("active-turn carried compact semantic summary is disabled")
    scope_id = str(getattr(plan.thread, "workspace_task_id", "") or "")
    tool_context = reconstructed_model_tool_context(
        list(plan.visible_records),
        agent=None,
        request_id=request.request_id,
        run_id=scope_id,
        task_id=scope_id,
    )
    handoff = native_carried_tool_handoff(
        tool_context,
        plan.visible_records,
        max_chars=config.max_input_chars,
    )
    replacement = summarize_live_tool_history(
        LiveToolHistorySummaryRequest(
            history=[RuntimeFactsTurn(handoff)],
            backend=getattr(agent, "backend", None),
            agent=agent,
            request_id=str(request.request_id or ""),
            run_id=scope_id,
            task_id=scope_id,
            task_prompt=str(request.task_prompt or "继续当前任务。"),
            previous_summary=str(plan.thread.summary or ""),
            max_output_chars=config.max_input_chars,
        )
    )
    if not replacement:
        raise ValueError("active-turn carried compact summary is empty")
    return replacement


# LLM: This helper is the sole checkpoint/CAS writer for the recovery candidate and forwards the
# same interrupt check. Progress at 92% remains between checkpoint append and CAS.
# 函数用途: 用冻结边界可中断地提交 active-turn Compact，并在恢复点后更新进度。
def _commit_active_turn_compact(
    agent: object,
    plan: _ActiveTurnArchiveCompactPlan,
    request: ActiveTurnArchiveCompactRequest,
    replacement: str,
    after_tokens: int,
) -> ConversationThread:
    return commit_live_tool_compact(
        agent,
        plan.binding,
        LiveToolCompactCommitRequest(
            summary=replacement,
            source_tool_call_ids=plan.source_call_ids,
            retained_tool_call_ids=plan.retained_call_ids,
            source_tool_call_refs=plan.source_call_refs,
            retained_tool_call_refs=plan.retained_call_refs,
            projected_tokens_before=plan.projected_tokens_before,
            projected_tokens_after=after_tokens,
            policy=plan.policy,
            request_id=str(request.request_id or ""),
            attempt_id=str(request.attempt_id or request.request_id or ""),
            forced=True,
            interrupt_check=request.interrupt_check,
            after_checkpoint=lambda: _emit_progress(
                request.progress_callback,
                plan.progress,
                phase="progress",
                stage="committing",
                percent=92,
                after_tokens=after_tokens,
            ),
        ),
    )


# LLM: The newest whole records are retained up to the shared recent-tail token budget. At least
# one older record remains eligible for replacement when possible; no record body is clipped.
# 函数用途: 从尾部选择仍直接给模型看的近期完整调用，超预算的旧调用交给摘要。
def _recent_records_within_budget(
    indexed: list[tuple[dict[str, object], str]],
    token_budget: int,
) -> list[tuple[dict[str, object], str]]:
    if len(indexed) <= 1:
        return []
    budget = max(1, int(token_budget or 0))
    selected: list[tuple[dict[str, object], str]] = []
    used = 0
    for item in reversed(indexed[1:]):
        cost = max(1, estimate_tokens(item[0]))
        if selected and used + cost > budget:
            break
        if not selected and cost > budget:
            break
        selected.append(item)
        used += cost
    selected.reverse()
    return selected


# LLM: The exact agent thread takes precedence over its parent conversation thread. This mirrors
# live-tool binding and prevents a delegated Compact from advancing the parent's generation.
# 函数用途: 按结构化属性加载本代理自己的唯一会话线程，缺失或损坏时停止恢复。
def _load_authoritative_thread(agent: object, task_attributes: object) -> ConversationThread:
    attrs = task_attributes if isinstance(task_attributes, Mapping) else {}
    thread_id = str(
        attrs.get(AGENT_THREAD_ID_ATTR) or attrs.get("conversation_thread_id") or ""
    ).strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        raise OSError("authoritative active turn has no conversation thread store")
    thread, load_error = store.threads.load_report(thread_id)
    if load_error is not None or thread is None:
        raise OSError("authoritative active turn conversation thread is unavailable")
    return thread


# LLM: UI progress is a volatile projection. Callback failure never cancels a checkpoint/CAS.
# 函数用途: 发出同一 compact 操作的阶段与百分比，断开的界面不会影响真实提交。
def _emit_progress(
    callback: Callable[[dict[str, object]], object] | None,
    base: dict[str, object],
    *,
    phase: str,
    stage: str,
    percent: int,
    after_tokens: int = 0,
    error_code: str = "",
) -> None:
    if callback is None:
        return
    payload = {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA,
        **base,
        "phase": str(phase),
        "stage": str(stage),
        "percent": min(100, max(0, int(percent or 0))),
        "after_tokens": max(0, int(after_tokens or 0)),
        "error_code": str(error_code or ""),
    }
    try:
        callback(payload)
    except Exception:
        return


# LLM: This helper accepts only the typed transcript-authority flag; user text cannot enable writes.
# 函数用途: 判断本轮是否允许把恢复压缩提交到唯一会话账本。
def _transcript_authoritative(attributes: object) -> bool:
    return bool(
        isinstance(attributes, Mapping)
        and attributes.get(CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR) is True
    )


# LLM: 这里只提取审计编号；过滤权威来自完整 ToolCallRef，不能用此函数补全来源。
# 函数用途: 保留已有归档的工具编号展示口径，不将别名解析成运行身份。
def _record_call_id(record: dict[str, object]) -> str:
    return str(record.get("call_id") or record.get("id") or "").strip()


__all__ = [
    "ActiveTurnArchiveCompactRequest",
    "ActiveTurnArchiveCompactResult",
    "compact_carried_active_turn_archive",
    "model_visible_active_turn_tool_calls",
]
