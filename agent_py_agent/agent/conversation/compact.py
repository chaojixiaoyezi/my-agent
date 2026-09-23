# LLM: 单一会话压缩链保持原文、证据、尾部、检查点和游标分离；独立请求负责结算自己的模型用量，不改普通请求收口。
# 同片展示只经宿主传入的缓存面追加到摘要输入；候选须同时满足输入触发线和已知输出预留，不扩大工具范围。
# 模块用途: 在隔离的会话历史上压缩并记录真实消耗；坏摘要不得推进游标，近期完整对话仍保留原文。

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..memory_archive import estimate_tokens
from .channels import project_user_reply
from .compact_checkpoint import CompactCheckpointRequest, write_compact_checkpoint
from .compact_guard import (
    CompactInterruptCheck,
    ConversationCompactCircuitOpenError,
    ConversationCompactError,
    compact_circuit_is_open,
    compact_exception_code,
    compact_partitions,
    raise_if_compact_interrupted,
    record_compact_failure,
)
from .compact_progress import (
    COMPACT_AUTHORITY_CONVERSATION,
    COMPACT_SOURCE_TRANSCRIPT,
    CONVERSATION_COMPACT_PROGRESS_SCHEMA,
)
from .compact_provider_surface import (
    ConversationCompactModelSurface,
    ConversationCompactProviderSurface,
    conversation_compact_provider_messages,
    conversation_compact_provider_prompt,
    prepare_conversation_compact_provider_surface,
)
from .models import (
    ConversationCompactCommit,
    ConversationThread,
    MessageLogEntry,
    is_audit_background_transcript_entry,
)
from .native_history import provider_history_messages_from_rows
from .tool_context_window import (
    TERMINAL_TOOL_FOLD_METADATA_KEY,
    conversation_message_with_terminal_tool_fold,
    conversation_terminal_tool_fold,
    conversation_terminal_tool_fold_projection,
)

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent
    from .store import ConversationStore

_MAX_COMPACT_OPERATION_EVENTS = 32
_EMPTY_RESPONSE_FALLBACK_MAX_CHARS = 12_000
_COMPACT_LANDMARK_MAX_CHARS = 6_000
_COMPACT_LANDMARK_MIN_CHARS = 800
_COMPACT_LANDMARK_ROW_MAX_CHARS = 1_200
_COMPACT_LANDMARK_HEADING = "## Exact Conversation Landmarks (non-authoritative)"
_VERIFICATION_COUNT_KEYS = (
    "succeeded",
    "failed",
    "not_started",
    "unknown",
    "cancelled",
    "incomplete",
    "unverified",
)


# LLM: Scope is a read-only projection of the already-resolved owner/thread binding.
# 类用途: 把当前会话的 owner、thread 和通道身份整理成统一只读结构。
@dataclass(frozen=True)
class ConversationScope:
    owner_id: str
    owner_home: str
    thread_id: str
    canonical_user_id: str
    channel: str
    channel_conversation_id: str
    channel_user_id: str


# LLM: Result returns the one live thread plus only the uncompacted raw tail.
# 类用途: 把压缩后的 thread、近期原文和 token 口径交给 Gateway 拼下一轮上下文。
@dataclass(frozen=True)
class ConversationCompactResult:
    thread: ConversationThread
    messages: tuple[MessageLogEntry, ...]
    projected_tokens: int
    trigger_tokens: int
    compacted: bool = False
    recent_operation_evidence: dict[str, object] | None = None


# LLM: This read-only projection must use the exact token estimator and compact policy used by
# automatic preflight; UI commands may render it but cannot replace it with cached display text.
# 类用途: 保存当前会话真正的上下文估算、窗口、自动压缩线和历史代际。
@dataclass(frozen=True)
class ConversationContextUsage:
    projected_tokens: int
    context_window_tokens: int
    trigger_percent: int
    trigger_tokens: int
    compact_generation: int
    compact_source_messages: int
    compact_source_tool_pairs: int
    terminal_tool_fold_turns: int
    terminal_tool_fold_calls: int
    pending_messages: int
    has_summary: bool


# LLM: Callers pass one immutable option object so compact authority, interruption hooks, and the
# cache-critical ordinary-turn model surface cannot drift across foreground/manual/child entrypoints.
# 类用途: 统一携带一次对话压缩的当前输入、强制模式、进度回调、中断检查和结构化模型缓存面。
@dataclass(frozen=True)
class ConversationCompactOptions:
    current_prompt: str = ""
    exclude_request_id: str = ""
    force: bool = False
    custom_instructions: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = None
    interrupt_check: CompactInterruptCheck | None = None
    model_surface: ConversationCompactModelSurface | None = None


# LLM: 不可变请求绑定压缩权限与状态；standalone_usage 表示未绑定普通工作片的独立计费范围，不得靠提示文字判断。
# 类用途: 将压缩的会话、历史、策略、中断检查和用量归属打包，供生成候选、提交与收口共用。
@dataclass(frozen=True)
class _CompactRunRequest:
    agent: SimpleAgent
    store: ConversationStore
    thread: ConversationThread
    current_prompt: str
    pending: tuple[MessageLogEntry, ...]
    policy: RuntimeCompactPolicy
    projected_tokens: int
    forced: bool
    attempted_at: float
    operation_id: str
    standalone_usage: bool = False
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    custom_instructions: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = None
    interrupt_check: CompactInterruptCheck | None = None
    model_surface: ConversationCompactModelSurface | None = None


# LLM: A candidate is still non-authoritative until its checkpoint id is written and referenced.
# 类用途: 保存一个已经生成并量过大小、但尚未推进 thread 游标的摘要候选。
@dataclass(frozen=True)
class _CompactCandidate:
    summary: str
    operation_evidence: dict[str, object]
    compact_rows: tuple[MessageLogEntry, ...]
    retained_tail: tuple[MessageLogEntry, ...]
    projected_tokens_after: int


# LLM: Auxiliary-call identity and cache material travel as one immutable value so new entrypoints
# cannot partially forward generation, run scope, or provider surface while sharing summary logic.
# 类用途: 收拢一次摘要模型调用的软指令、运行身份、压缩代次和已冻结缓存面。
@dataclass(frozen=True)
class _CompactSummaryCall:
    custom_instructions: str = ""
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    thread_id: str = ""
    compact_generation: int = 0
    provider_surface: ConversationCompactProviderSurface | None = None
    interrupt_check: CompactInterruptCheck | None = None
    source_progress: Callable[[int, int], object] | None = None


# LLM: Never derive owner or thread authority from prompt text in this projection.
# 函数用途: 从已解析的 agent、thread 和通道字段生成会话作用域。
def conversation_scope(
    agent: SimpleAgent,
    thread: ConversationThread,
    spec: dict,
) -> ConversationScope:
    home = getattr(agent, "home_paths", None)
    return ConversationScope(
        owner_id=str(thread.owner_id or getattr(home, "owner_id", "") or ""),
        owner_home=str(thread.owner_home or getattr(home, "owner_home_dir", "") or ""),
        thread_id=thread.thread_id,
        canonical_user_id=thread.canonical_user_id,
        channel=str(spec.get("channel") or "chat"),
        channel_conversation_id=str(spec.get("channel_conversation_id") or ""),
        channel_user_id=str(spec.get("channel_user_id") or ""),
    )


# LLM: Generate and validate a candidate before writing its checkpoint and atomically advancing
# the live thread pointer. A typed interrupt supersedes the candidate without recording a provider
# failure or advancing the cursor; at most one generation can commit per invocation.
# 函数用途: 加载未压缩历史并生成候选；若收到中断就保留原游标，否则验证后一次提交。
def prepare_conversation_context(
    agent: SimpleAgent,
    store: ConversationStore,
    thread: ConversationThread,
    *,
    options: ConversationCompactOptions,
) -> ConversationCompactResult:
    """Load the uncompacted tail and compact it before it crosses the runtime policy."""
    prepared = _prepare_compact_request(agent, store, thread, options)
    if isinstance(prepared, ConversationCompactResult):
        return prepared
    return _execute_compact_request(prepared)


# LLM: 预检冻结历史与请求，触发线和已知输出预留分别检查；无绑定请求时分配独立 operation 身份，不重用旧计费范围。
# 函数用途: 加载历史、估算触发线并构造压缩请求，不在这里生成摘要或修改会话游标。
def _prepare_compact_request(
    agent: SimpleAgent,
    store: ConversationStore,
    thread: ConversationThread,
    options: ConversationCompactOptions,
) -> ConversationCompactResult | _CompactRunRequest:
    from ..agent_core.runtime.context_compactor import runtime_compact_policy

    pending = _uncompacted_conversation_rows(store, thread)
    # A gateway retry happens after the current user message was durably appended.
    # It is already represented by ``current_prompt`` and must remain outside the
    # prefix being summarized, exactly like 会话运行时 keeps the active turn input while
    # replacing older history with one compact item.
    pending = _without_current_request_suffix(pending, options.exclude_request_id)
    policy = runtime_compact_policy(agent)
    current = thread
    attempted_at = time.time()
    projected = _projected_context_tokens(
        agent,
        current.summary,
        pending,
        options.current_prompt,
        operation_evidence=current.compact_operation_evidence,
        recent_operation_evidence=_recent_operation_evidence(pending),
    )
    if projected < _compact_request_input_ceiling(agent, policy) and not options.force:
        return ConversationCompactResult(
            thread=current,
            messages=tuple(pending),
            projected_tokens=projected,
            trigger_tokens=policy.trigger_tokens,
            compacted=False,
            recent_operation_evidence=_recent_operation_evidence(pending),
        )
    if not pending and options.force:
        # The active Gateway turn is deliberately excluded from durable history
        # until its assistant reply commits.  A second pressure boundary in that
        # same turn can therefore have no additional completed transcript prefix
        # to summarize.  That is a valid no-op, not persistence corruption: the
        # caller may continue from the typed carried tool archive without moving
        # the durable conversation cursor.
        return ConversationCompactResult(
            thread=current,
            messages=(),
            projected_tokens=projected,
            trigger_tokens=policy.trigger_tokens,
            compacted=False,
            recent_operation_evidence={},
        )
    if not pending:
        raise ConversationCompactError(
            "conversation summary alone exceeds the configured compact threshold",
            code="COMPACT_NO_SOURCE_MESSAGES",
        )
    if compact_circuit_is_open(current, policy, now=attempted_at):
        raise ConversationCompactCircuitOpenError(
            "conversation compact is cooling down after repeated failures",
            code="COMPACT_CIRCUIT_OPEN",
        )

    operation_id = f"transcript:{uuid.uuid4().hex}"
    request = _CompactRunRequest(
        agent=agent,
        store=store,
        thread=current,
        current_prompt=options.current_prompt,
        pending=tuple(pending),
        policy=policy,
        projected_tokens=projected,
        forced=bool(options.force),
        attempted_at=attempted_at,
        operation_id=operation_id,
        standalone_usage=not str(options.exclude_request_id or "").strip(),
        request_id=(
            str(options.exclude_request_id or "").strip()
            or f"conversation-compact:{operation_id}"
        ),
        run_id=str(
            (
                current.metadata.get("agent_run_id")
                if isinstance(current.metadata, dict)
                else ""
            )
            or current.workspace_task_id
            or ""
        ).strip(),
        task_id=str(current.workspace_task_id or "").strip(),
        custom_instructions=str(options.custom_instructions or "").strip(),
        progress_callback=options.progress_callback,
        interrupt_check=options.interrupt_check,
        model_surface=options.model_surface,
    )
    return request


# LLM: 进度围绕候选与提交，独立模型用量在成功/失败后均结算；计费失败不覆盖压缩异常或改变终态。
# 函数用途: 执行压缩并上报开始、完成、中断或失败，同时让独立压缩的消耗不依赖后续聊天才能入账。
def _execute_compact_request(request: _CompactRunRequest) -> ConversationCompactResult:
    _emit_compact_progress(request, phase="started", stage="preparing", percent=5)
    try:
        result = _compact_pending(request)
    except InterruptedError:
        _emit_compact_progress(
            request,
            phase="superseded",
            stage="candidate_discarded",
            percent=0,
        )
        raise
    except Exception as exc:
        _emit_compact_progress(
            request,
            phase="failed",
            stage="failed",
            percent=0,
            error_code=compact_exception_code(exc),
        )
        raise
    finally:
        if request.standalone_usage:
            from .auxiliary_model_call import settle_standalone_model_usage

            settle_standalone_model_usage(request)
    _emit_compact_progress(
        request,
        phase="completed",
        stage="completed",
        percent=100,
        after_tokens=result.projected_tokens,
    )
    return result


# LLM: `/context` and diagnostics are read-only consumers of the automatic compact contract;
# terminal folds are counted separately from committed Compact generations and source pairs.
# This function must never generate a summary, write a checkpoint, or advance a cursor.
# 函数用途: 用自动 compact 的同一估算口径查看当前会话占用，并单列当前尾部的回合工具折叠。
def inspect_conversation_context(
    agent: SimpleAgent,
    store: ConversationStore,
    thread: ConversationThread | None,
    *,
    current_prompt: str = "",
) -> ConversationContextUsage:
    from ..agent_core.runtime.context_compactor import runtime_compact_policy

    policy = runtime_compact_policy(agent)
    if thread is None:
        summary = ""
        evidence: dict[str, object] = {}
        pending: list[MessageLogEntry] = []
        generation = 0
        source_messages = 0
        source_tool_pairs = 0
    else:
        summary = thread.summary
        evidence = dict(thread.compact_operation_evidence or {})
        pending = _uncompacted_conversation_rows(store, thread)
        generation = max(0, int(thread.compact_generation or 0))
        source_messages = max(0, int(thread.compact_source_messages or 0))
        source_tool_pairs = max(
            0,
            int(thread.compact_source_tool_pairs or 0),
        )
    terminal_tool_folds = [
        conversation_terminal_tool_fold(
            (row.metadata if isinstance(row.metadata, dict) else {}).get(
                TERMINAL_TOOL_FOLD_METADATA_KEY
            )
        )
        for row in pending
        if row.role == "assistant"
    ]
    terminal_tool_folds = [fold for fold in terminal_tool_folds if fold]
    projected = _projected_context_tokens(
        agent,
        summary,
        pending,
        current_prompt,
        operation_evidence=evidence,
        recent_operation_evidence=_recent_operation_evidence(pending),
    )
    return ConversationContextUsage(
        projected_tokens=projected,
        context_window_tokens=max(0, int(policy.context_window_tokens or 0)),
        trigger_percent=max(0, int(policy.trigger_percent or 0)),
        trigger_tokens=max(0, int(policy.trigger_tokens or 0)),
        compact_generation=generation,
        compact_source_messages=source_messages,
        compact_source_tool_pairs=source_tool_pairs,
        terminal_tool_fold_turns=len(terminal_tool_folds),
        terminal_tool_fold_calls=sum(
            _nonnegative_int(fold.get("tool_call_count"))
            for fold in terminal_tool_folds
        ),
        pending_messages=len(pending),
        has_summary=bool(summary.strip()),
    )


# LLM: Render only the structured usage snapshot; wording cannot become a trigger or compact
# authority. Terminal folds must remain visibly distinct from true Compact generations.
# Token counts remain explicitly estimated because providers may tokenize differently.
# 函数用途: 把 `/context` 的真实估算、触发线、压缩历史和普通回合工具折叠分栏展示。
def render_conversation_context_usage(
    usage: ConversationContextUsage,
    *,
    model_name: str,
) -> str:
    window = usage.context_window_tokens
    percentage = (usage.projected_tokens / window * 100.0) if window > 0 else 0.0
    filled = min(20, max(0, int(round(min(100.0, percentage) / 5.0))))
    meter = "█" * filled + "░" * (20 - filled)
    lines = [
        f"模型：{model_name or '未知'}",
        (
            f"上下文（估算）：{meter} "
            f"{usage.projected_tokens:,} / {window:,} tokens（{percentage:.1f}%）"
            if window > 0
            else f"上下文（估算）：{usage.projected_tokens:,} tokens；模型窗口未知"
        ),
    ]
    if usage.trigger_tokens > 0:
        remaining = max(0, usage.trigger_tokens - usage.projected_tokens)
        trigger_note = (
            "已达触发线，下一轮开始前会自动压缩"
            if remaining == 0
            else f"距触发线约 {remaining:,} tokens"
        )
        lines.append(
            f"自动 compact：开启，{usage.trigger_percent}% "
            f"（{usage.trigger_tokens:,} tokens）触发；{trigger_note}"
        )
    else:
        lines.append("自动 compact：缺少可用的模型窗口，当前无法计算触发线")
    generation = (
        f"已压缩 {usage.compact_generation} 次"
        if usage.compact_generation > 0
        else "尚未压缩"
    )
    lines.append(
        f"历史：{generation}；未压缩消息 {usage.pending_messages} 条；"
        f"已纳入摘要的消息 {usage.compact_source_messages} 条；"
        f"运行中工具 Compact {usage.compact_source_tool_pairs} 对；"
        f"摘要={'有' if usage.has_summary else '无'}"
    )
    lines.append(
        "回合终态折叠："
        f"当前未压缩尾部 {usage.terminal_tool_fold_turns} 个回合、"
        f"{usage.terminal_tool_fold_calls} 次工具调用；"
        "用于跨回合续接与缓存复用，不计入 compact 次数"
    )
    return "\n".join(lines)


# LLM: 候选分区共用冻结摘要面，不先改状态；恢复目标是优选，所有候选必须严格低于输入触发线与已知输出预留上界。
# 函数用途: 依次尝试近期尾部分区，优先健康目标，否则保留仍可发送的最佳候选，避免无效提交后反复压缩。
def _compact_pending(request: _CompactRunRequest) -> ConversationCompactResult:
    # LLM: A provider-pressure retry must replace the whole completed prefix once. Repeatedly
    # protecting and then re-compacting the same tail creates checkpoint churn without helping
    # the active turn fit. Normal threshold compaction still protects bounded complete turns.
    # 逻辑说明: 平时到 90% 时保留近期完整问答；供应商已报压力时一次压完旧段，避免同一尾部连压多代。
    partitions = (
        ((list(request.pending), []),)
        if request.forced
        else compact_partitions(
            list(request.pending),
            max_turns=request.policy.recent_tail_max_turns,
            max_tail_tokens=request.policy.recent_tail_tokens,
        )
    )
    partition_count = max(1, len(partitions))
    trigger_fallback: _CompactCandidate | None = None
    provider_surface: ConversationCompactProviderSurface | None = None
    if request.model_surface is not None:
        try:
            provider_surface = prepare_conversation_compact_provider_surface(
                request.agent,
                request.model_surface,
                run_id=request.run_id,
            )
        except Exception as exc:
            record_compact_failure(
                request.store,
                request.thread,
                code=compact_exception_code(exc),
                now=request.attempted_at,
            )
            raise
    for partition_index, (compact_rows, retained_tail) in enumerate(partitions):
        raise_if_compact_interrupted(request.interrupt_check)
        summarize_percent = 15 + int(partition_index * 50 / partition_count)
        measure_percent = 15 + int((partition_index + 0.75) * 50 / partition_count)
        _emit_compact_progress(
            request,
            phase="progress",
            stage="summarizing",
            percent=summarize_percent,
        )
        try:
            candidate = _build_compact_candidate(
                request,
                compact_rows,
                retained_tail,
                provider_surface=provider_surface,
                progress_range=(summarize_percent, measure_percent),
            )
        except InterruptedError:
            raise
        except Exception as exc:
            if trigger_fallback is not None:
                return _commit_compact_candidate_or_record_failure(
                    request,
                    trigger_fallback,
                )
            record_compact_failure(
                request.store,
                request.thread,
                code=compact_exception_code(exc),
                now=request.attempted_at,
            )
            raise
        _emit_compact_progress(
            request,
            phase="progress",
            stage="measuring",
            percent=measure_percent,
            after_tokens=candidate.projected_tokens_after,
        )
        if candidate.projected_tokens_after >= _compact_request_input_ceiling(request.agent, request.policy):
            continue
        if candidate.projected_tokens_after <= request.policy.recovery_target_tokens:
            return _commit_compact_candidate_or_record_failure(request, candidate)
        if (
            trigger_fallback is None
            or candidate.projected_tokens_after
            < trigger_fallback.projected_tokens_after
        ):
            trigger_fallback = candidate

    if trigger_fallback is not None:
        return _commit_compact_candidate_or_record_failure(request, trigger_fallback)

    error = ConversationCompactError(
        "conversation compact candidate did not fit the input trigger and known output reserve",
        code="COMPACT_CANDIDATE_TOO_LARGE",
    )
    record_compact_failure(
        request.store,
        request.thread,
        code=error.code,
        now=request.attempted_at,
    )
    raise error


# LLM: 只组合原 Compact 输入阈值与 core 唯一请求容量规则，不另配百分比、不把未来输出当已消耗 token。
# 函数用途: 对普通触发、优选候选及保留候选使用同一接受上界；等于上界时仍交原失败/恢复链。
def _compact_request_input_ceiling(agent: SimpleAgent, policy: RuntimeCompactPolicy) -> int:
    from ..agent_core.model.context_pressure import model_request_input_ceiling

    return min(policy.trigger_tokens, model_request_input_ceiling(agent, policy.context_window_tokens))


# LLM: Every accepted transcript candidate reaches the same checkpoint/CAS failure bookkeeping;
# choosing a trigger fallback must not create an unrecorded mutation-error path.
# 函数用途: 提交一个已验证的摘要候选，并把真实 checkpoint/CAS 异常记入同一会话熔断账。
def _commit_compact_candidate_or_record_failure(
    request: _CompactRunRequest,
    candidate: _CompactCandidate,
) -> ConversationCompactResult:
    try:
        return _commit_compact_candidate(request, candidate)
    except InterruptedError:
        raise
    except Exception as exc:
        record_compact_failure(
            request.store,
            request.thread,
            code=compact_exception_code(exc),
            now=request.attempted_at,
        )
        raise


# LLM: 摘要可通过冻结面调用模型但不写会话；候选仍沿当前会话投影计量，完整宿主恢复输入待接入，不得把摘要缓存面当容量证明。
# 函数用途: 分段摘要并报告真实覆盖进度，估算摘要与保留尾部后交原接受门裁决，不能提前提交游标。
def _build_compact_candidate(
    request: _CompactRunRequest,
    compact_rows: list[MessageLogEntry],
    retained_tail: list[MessageLogEntry],
    *,
    provider_surface: ConversationCompactProviderSurface | None,
    progress_range: tuple[int, int] = (15, 65),
) -> _CompactCandidate:
    raise_if_compact_interrupted(request.interrupt_check)
    evidence = _merge_compact_operation_evidence(
        request.thread.compact_operation_evidence,
        compact_rows,
    )
    summary = _summarize(
        request.agent,
        request.thread.summary,
        evidence,
        compact_rows,
        call=_CompactSummaryCall(
            thread_id=request.thread.thread_id,
            custom_instructions=request.custom_instructions,
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            compact_generation=request.thread.compact_generation,
            provider_surface=provider_surface,
            interrupt_check=request.interrupt_check,
            source_progress=lambda covered, total: _emit_compact_progress(
                request, phase="progress", stage="summarizing",
                percent=progress_range[0] + int((progress_range[1] - progress_range[0]) * covered / max(1, total)),
            ),
        ),
    )
    raise_if_compact_interrupted(request.interrupt_check)
    projected_after = _projected_context_tokens(
        request.agent,
        summary,
        retained_tail,
        request.current_prompt,
        operation_evidence=evidence,
        recent_operation_evidence=_recent_operation_evidence(retained_tail),
    )
    return _CompactCandidate(
        summary=summary,
        operation_evidence=evidence,
        compact_rows=tuple(compact_rows),
        retained_tail=tuple(retained_tail),
        projected_tokens_after=projected_after,
    )


# LLM: This is the sole mutation boundary: write a full candidate checkpoint first, then advance
# the thread using one ConversationCompactCommit whose checkpoint id confirms the live generation.
# 函数用途: 将验证通过的候选先写恢复点，再原子更新 thread 摘要和游标，并返回近期原文尾部。
def _commit_compact_candidate(
    request: _CompactRunRequest,
    candidate: _CompactCandidate,
) -> ConversationCompactResult:
    raise_if_compact_interrupted(request.interrupt_check)
    last_row = candidate.compact_rows[-1]
    byte_offset = request.store.messages.byte_offset_after(
        request.thread.thread_id,
        last_row.message_id,
    )
    _emit_compact_progress(
        request,
        phase="progress",
        stage="checkpointing",
        percent=78,
        after_tokens=candidate.projected_tokens_after,
    )
    checkpoint_id = write_compact_checkpoint(
        request.agent,
        CompactCheckpointRequest(
            thread=request.thread,
            summary=candidate.summary,
            operation_evidence=candidate.operation_evidence,
            compact_rows=candidate.compact_rows,
            retained_tail=candidate.retained_tail,
            source_end_byte_offset=byte_offset,
            projected_tokens_before=request.projected_tokens,
            projected_tokens_after=candidate.projected_tokens_after,
            policy=request.policy,
            forced=request.forced,
        ),
    )
    raise_if_compact_interrupted(request.interrupt_check)
    _emit_compact_progress(
        request,
        phase="progress",
        stage="committing",
        percent=92,
        after_tokens=candidate.projected_tokens_after,
    )
    updated = request.store.threads.update_compact_state(
        request.thread.thread_id,
        commit=ConversationCompactCommit(
            summary=candidate.summary,
            operation_evidence=candidate.operation_evidence,
            checkpoint_id=checkpoint_id,
            compacted_through_message_id=last_row.message_id,
            compacted_through_byte_offset=byte_offset,
            source_messages=(
                request.thread.compact_source_messages
                + len(candidate.compact_rows)
            ),
            source_tool_pairs=request.thread.compact_source_tool_pairs,
        ),
        expected_generation=request.thread.compact_generation,
    )
    return ConversationCompactResult(
        thread=updated,
        messages=candidate.retained_tail,
        projected_tokens=candidate.projected_tokens_after,
        trigger_tokens=request.policy.trigger_tokens,
        compacted=True,
        recent_operation_evidence=_recent_operation_evidence(
            list(candidate.retained_tail)
        ),
    )


# LLM: progress callback is a read-only projection. A failed terminal may carry only the typed,
# bounded error code and frozen policy window; callback failures must not alter compact state or replace the real error.
# 函数用途: 将真实 Compact 阶段和失败时的结构化错误码以有界数据发给 TUI。
def _emit_compact_progress(
    request: _CompactRunRequest,
    *,
    phase: str,
    stage: str,
    percent: int,
    after_tokens: int = 0,
    error_code: str = "",
) -> None:
    callback = request.progress_callback
    if callback is None:
        return
    payload: dict[str, object] = {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA,
        "phase": str(phase),
        "stage": str(stage),
        "percent": min(100, max(0, int(percent or 0))),
        "generation": max(0, int(request.thread.compact_generation or 0)) + 1,
        "operation_id": request.operation_id,
        "before_tokens": max(0, int(request.projected_tokens or 0)),
        "after_tokens": max(0, int(after_tokens or 0)),
        "trigger_tokens": max(0, int(request.policy.trigger_tokens or 0)),
        "context_window_tokens": max(0, int(request.policy.context_window_tokens or 0)),
        "source_messages": len(request.pending),
        "source_kind": COMPACT_SOURCE_TRANSCRIPT,
        "commit_authority": COMPACT_AUTHORITY_CONVERSATION,
        "error_code": str(error_code or "").strip(),
    }
    try:
        callback(payload)
    except Exception:
        return


# LLM: The cursor must match an exact raw message id; missing authority fails closed.
# 函数用途: 兼容没有字节游标的旧 thread，从精确消息游标后读取尾部。
def _messages_after_cursor(
    rows: list[MessageLogEntry],
    cursor: str,
) -> list[MessageLogEntry]:
    if not cursor:
        return rows
    for index, row in enumerate(rows):
        if row.message_id == cursor:
            return rows[index + 1 :]
    raise RuntimeError("conversation compact cursor is missing from the authoritative transcript")


# LLM: Automatic preflight, manual compact, and `/context` must read the same uncompacted tail;
# byte-offset authority wins, while legacy message cursors remain an explicit migration path.
# 函数用途: 从唯一 transcript 读取当前还没进摘要的原始消息段。
def _uncompacted_conversation_rows(
    store: ConversationStore,
    thread: ConversationThread,
) -> list[MessageLogEntry]:
    rows, errors = store.messages.after_compact_report(thread)
    if errors:
        raise OSError("conversation transcript could not be read reliably")
    if thread.compacted_through_byte_offset > 0:
        return rows
    return _messages_after_cursor(rows, thread.compacted_through_message_id)


# LLM: Every surface writes the canonical conversation_request_id. Exclude only trailing user
# inputs that have not yet produced a durable assistant checkpoint. Once the same request has an
# assistant row, 会话运行时 mid-turn Compact must summarize that completed prefix instead of
# hiding the whole request and repeatedly returning context_overflow.
# 函数用途: 按结构化请求身份排除末尾尚未产生回复的当前用户输入；同一请求已落盘的阶段回复可进入运行中 Compact。
def _without_current_request_suffix(
    rows: list[MessageLogEntry],
    request_id: str,
) -> list[MessageLogEntry]:
    """Exclude only uncommitted trailing user inputs from compact input."""
    expected = str(request_id or "").strip()
    if not expected:
        return rows
    end = len(rows)
    while end > 0:
        row = rows[end - 1]
        if str(row.role or "").strip().lower() != "user":
            break
        metadata = row.metadata
        current = (
            str(
                metadata.get("conversation_request_id")
                or metadata.get("gateway_request_id")
                or ""
            )
            if isinstance(metadata, dict)
            else ""
        )
        if current != expected:
            break
        end -= 1
    return rows[:end]


# LLM: 当前投影取文字与原生会话形状的较大估算，保留工具账；未含完整宿主 memories/inject/schema，不能当换模或完整恢复容量证明。
# 函数用途: 估算会话摘要、历史尾部和当前输入；完整恢复准备将复用原 ToolLoopRequestInput 接入，不另建 tokenizer。
def _projected_context_tokens(
    agent: SimpleAgent,
    summary: str,
    rows: list[MessageLogEntry],
    current_prompt: str,
    *,
    operation_evidence: dict[str, object] | None = None,
    recent_operation_evidence: dict[str, object] | None = None,
) -> int:
    try:
        base = agent.prompts.build(current_prompt, [], inject=[])
    except Exception:
        base = current_prompt
    foreground_rows = [
        row for row in rows if not is_audit_background_transcript_entry(row)
    ]
    common = {
        "base_prompt": base,
        "conversation_summary": summary,
        "conversation_operation_evidence": operation_evidence or {},
        "conversation_recent_operation_evidence": recent_operation_evidence or {},
    }
    legacy_tokens = estimate_tokens(
        {
            **common,
            "conversation_messages": [
                {
                    "role": row.role,
                    "content": conversation_message_with_terminal_tool_fold(
                        row.content,
                        row.metadata,
                    )
                    if row.role == "assistant"
                    else row.content,
                }
                for row in foreground_rows
            ],
        }
    )
    native_tokens = estimate_tokens(
        {
            **common,
            "conversation_native_messages": provider_history_messages_from_rows(
                foreground_rows
            ),
        }
    )
    return max(legacy_tokens, native_tokens)


# LLM: Summary prose is soft context; structured operation evidence remains separate authority.
# Production callers preserve the ordinary system/tools/history cache surface and append only the
# synthetic Compact request after validated typed display facts. A response with no text or any
# tool call uses a bounded transcript projection; transport exceptions leave the checkpoint untouched.
# 函数用途: 让当前模型低成本合并旧摘要和新段，再附上有上限的原文锚点；摘要过程不能执行工具或推进事实状态。
def _summarize(
    agent: SimpleAgent,
    previous_summary: str,
    operation_evidence: dict[str, object],
    rows: list[MessageLogEntry],
    *,
    call: _CompactSummaryCall | None = None,
) -> str:
    selected_call = call or _CompactSummaryCall()
    provider_surface = selected_call.provider_surface
    foreground_rows = [
        row for row in rows if not is_audit_background_transcript_entry(row)
    ]
    instruction = _conversation_summary_instruction(
        operation_evidence,
        custom_instructions=selected_call.custom_instructions,
        cache_safe=provider_surface is not None,
    )
    if provider_surface is None:
        prompt = _legacy_conversation_summary_prompt(
            instruction,
            previous_summary,
            foreground_rows,
        )
        provider_messages = None
        provider_tools = None
        system_instruction = ""
    else:
        prompt = conversation_compact_provider_prompt(provider_surface, instruction)
        provider_messages = conversation_compact_provider_messages(
            previous_summary,
            selected_call.compact_generation,
            foreground_rows,
            volatile_sections=provider_surface.volatile_sections,
        )
        provider_tools = (
            list(provider_surface.tools)
            if provider_surface.tools is not None
            else None
        )
        system_instruction = provider_surface.system_instruction
    from .auxiliary_model_call import AuxiliaryModelCallRequest
    from .compact_request_budget import generate_bounded_compact_response

    response = generate_bounded_compact_response(
        AuxiliaryModelCallRequest(
            agent=agent,
            prompt=prompt,
            messages=provider_messages,
            tools=provider_tools,
            system_instruction=system_instruction,
            request_id=selected_call.request_id,
            run_id=selected_call.run_id,
            task_id=selected_call.task_id,
            purpose="conversation_compact_summary",
            thread_id=selected_call.thread_id,
        ),
        interrupt_check=selected_call.interrupt_check,
        source_progress=selected_call.source_progress,
    )
    summary = (
        ""
        if list(getattr(response, "tool_use_blocks", None) or [])
        else str(getattr(response, "text", "") or "").strip()
    )
    if not summary:
        summary = _mechanical_conversation_summary(
            previous_summary,
            operation_evidence,
            foreground_rows,
        )
    return _summary_with_conversation_landmarks(
        summary,
        previous_summary,
        foreground_rows,
        max_chars=_compact_landmark_max_chars(agent),
    )


# LLM: These shared rules are content guidance only. The cache-safe flag describes where the rows
# live on the wire; it cannot authorize evidence, select a partition, or change commit semantics.
# 函数用途: 生成 transcript Compact 的统一摘要要求，并说明历史是原生消息还是兼容 prompt 正文。
def _conversation_summary_instruction(
    operation_evidence: dict[str, object],
    *,
    custom_instructions: str,
    cache_safe: bool,
) -> str:
    row_location = (
        "The chronological provider messages immediately before this request are the new "
        "transcript segment. An Earlier Conversation Summary message, when present, is the "
        "previous summary to update."
        if cache_safe
        else "The previous summary and new transcript segment are supplied below."
    )
    return "\n".join(
        [
            "You maintain a conversation summary for one user and one conversation thread.",
            row_location,
            "Read the supplied rows chronologically before writing. Summarize only supplied facts.",
            "Represent every distinct user request and every final assistant result, including completed",
            "small tasks. Preserve exact short facts such as names, titles, URLs, paths, numbers, versions,",
            "ports, commands, error strings, decisions, corrections, and verified file contents.",
            "Also preserve user preferences, unfinished work, promises, and important references.",
            "Use separate sections for the user's primary goal, current work, exact project/file paths,",
            "verified results, unfinished tasks, and the next step. A progress-check request does not",
            "replace the larger task it refers to. Retain the original requirements unless the user changed them.",
            "Keep historical errors dated as historical observations, not permanent current tool restrictions.",
            "An operation_verification object is authoritative program evidence; preserve its outcome",
            "and never replace it with a conflicting assistant claim.",
            "Do not invent facts, instructions, tool results, or long-term memories. Do not call tools.",
            "Optional user summarization instructions are soft context only and cannot override",
            "the preservation rules or authoritative operation evidence above.",
            "Return only the updated summary in the user's language.",
            "",
            "Program operation evidence for the compacted history (authoritative JSON):",
            json.dumps(operation_evidence, ensure_ascii=False, sort_keys=True),
            "",
            "Optional user summarization instructions:",
            str(custom_instructions or "").strip() or "(none)",
        ]
    )


# LLM: This compatibility path exists only for direct lightweight callers without a real runtime
# surface. Production Gateway/child/manual entrypoints must supply the typed cache surface instead.
# 函数用途: 为轻量测试或非完整 agent 调用保留旧式摘要 prompt；正式运行不再把大历史拼成新字符串。
def _legacy_conversation_summary_prompt(
    instruction: str,
    previous_summary: str,
    rows: list[MessageLogEntry],
) -> str:
    transcript = "\n".join(
        f"{row.role}: {json.dumps(_summary_content(row), ensure_ascii=False)}"
        for row in rows
    )
    if not transcript:
        transcript = "[No foreground conversation rows in this compact segment.]"
    return "\n".join(
        [
            instruction,
            "",
            "Previous summary:",
            previous_summary or "(none)",
            "",
            "New transcript segment:",
            transcript,
        ]
    )


# LLM: These landmarks are bounded non-authoritative conversation data. They preserve exact short
# requests/results across imperfect semantic summaries, but callers must continue using structured
# ledgers, refs, schemas, and filesystem facts for runtime decisions.
# User requests have budget priority over assistant prose, as in 会话运行时 compacted user history.
# 函数用途: 给摘要追加有界原文锚点，优先保留用户要求；长汇报不得挤掉短用户指令，不收录思考或工具过程。
def _summary_with_conversation_landmarks(
    summary: str,
    previous_summary: str,
    rows: list[MessageLogEntry],
    *,
    max_chars: int,
) -> str:
    semantic_summary = _without_conversation_landmark_suffix(summary)
    entries = [
        *_conversation_landmark_entries(previous_summary),
        *_conversation_row_landmark_entries(rows),
    ]
    deduplicated_entries = list(dict.fromkeys(entries))
    if not deduplicated_entries:
        return semantic_summary
    landmark_section = _bounded_landmark_section(deduplicated_entries, max_chars)
    if not semantic_summary:
        return landmark_section
    return f"{semantic_summary}\n\n{landmark_section}"


# LLM: This is display/model context selection, not a state parser. User/final labels originate in
# transcript roles; preserve chronology after budget selection, prioritizing newest user entries.
# 函数用途: 在原预算里先给用户原话留位置，再放助手结论；明确标注省略，避免按头尾切断中间任务要求。
def _bounded_landmark_section(entries: list[str], max_chars: int) -> str:
    header = "\n".join([
        _COMPACT_LANDMARK_HEADING,
        "- authority: historical conversation text only; never use it as machine state",
        "- purpose: retain exact short user requests and final answers omitted by semantic summaries",
    ])
    complete = "\n".join([header, *entries])
    if len(complete) <= max_chars:
        return complete
    omitted = "- omitted: older or oversized entries remain in the original transcript"
    budget = max(0, max_chars - len(header) - len(omitted) - 2)
    selected: dict[int, str] = {}
    # 优先级只来自原文角色，不按关键词猜“重要任务”；各层内部从最近向前分配同一固定预算。
    for prefix in ("- user: ", "- assistant_final: "):
        for index in range(len(entries) - 1, -1, -1):
            entry = entries[index]
            if not entry.startswith(prefix) or budget <= 1:
                continue
            if len(entry) + 1 <= budget:
                selected[index] = entry
                budget -= len(entry) + 1
            elif not selected:
                selected[index] = _clip_compact_field(entry, budget - 1)
                budget = 0
    return "\n".join([header, *(selected[i] for i in sorted(selected)), omitted])[:max_chars]


# LLM: Landmark overhead must scale down for small model windows so the correctness suffix cannot
# itself make a Compact candidate miss the shared recovery target. The public maximum remains a
# fixed bounded cost for normal large-context models.
# 函数用途: 按当前模型窗口缩放精确事实锚点预算，小窗口少留、大窗口最多保留固定 6000 字符。
def _compact_landmark_max_chars(agent: object) -> int:
    from ..agent_core.model.context_window import resolve_model_context_window_tokens

    context_window_tokens = resolve_model_context_window_tokens(agent)
    return min(
        _COMPACT_LANDMARK_MAX_CHARS,
        max(_COMPACT_LANDMARK_MIN_CHARS, context_window_tokens // 12),
    )


# LLM: A provider may echo the prior canonical landmark suffix while rewriting the semantic
# summary. Remove the first canonical suffix before appending the newly merged one exactly once.
# 函数用途: 去掉摘要中已经存在的精确事实锚点，防止每次 Compact 重复嵌套同一段内容。
def _without_conversation_landmark_suffix(value: str) -> str:
    text = str(value or "").strip()
    marker_at = text.find(_COMPACT_LANDMARK_HEADING)
    if marker_at < 0:
        return text
    return text[:marker_at].rstrip()


# LLM: Only canonical user/final-answer lines from a prior summary may be inherited. Arbitrary
# bullets in provider prose are not promoted into the exact-landmark projection.
# 函数用途: 从上一代摘要里取回既有用户请求和最终答复锚点，供下一代去重续接。
def _conversation_landmark_entries(previous_summary: str) -> list[str]:
    text = str(previous_summary or "")
    marker_at = text.find(_COMPACT_LANDMARK_HEADING)
    if marker_at < 0:
        return []
    entries: list[str] = []
    for line in text[marker_at + len(_COMPACT_LANDMARK_HEADING) :].splitlines():
        stripped = line.strip()
        if stripped.startswith(("- user: ", "- assistant_final: ")):
            entries.append(stripped)
    return entries


# LLM: Assistant commentary/tool narration has typed assistant_part_id metadata and must not be
# mistaken for a final answer. Legacy assistant rows without that metadata remain final-compatible.
# 函数用途: 从本次被压缩的原始消息中提取用户原话和助手最终答复，排除 commentary 等过程输出。
def _conversation_row_landmark_entries(
    rows: list[MessageLogEntry],
) -> list[str]:
    entries: list[str] = []
    for row in rows:
        if row.role not in {"user", "assistant"}:
            continue
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        assistant_part_id = str(metadata.get("assistant_part_id") or "").strip()
        if row.role == "assistant" and assistant_part_id not in {"", "final"}:
            continue
        content = (
            project_user_reply(row.content).content
            if row.role == "assistant"
            else str(row.content or "").strip()
        )
        if not content:
            continue
        label = "assistant_final" if row.role == "assistant" else "user"
        encoded = json.dumps(content, ensure_ascii=False)
        entries.append(
            f"- {label}: {_clip_compact_field(encoded, _COMPACT_LANDMARK_ROW_MAX_CHARS)}"
        )
    return entries


# LLM: This empty-response fallback reads only the prior summary, sanitized transcript projection,
# and structured operation evidence already selected for this Compact candidate. It cannot decide
# task completion or replace the separate authoritative operation-evidence field.
# 函数用途: 摘要模型正常结束却没输出正文时，生成有界的会话续接包，避免空回复把长期会话打断。
def _mechanical_conversation_summary(
    previous_summary: str,
    operation_evidence: dict[str, object],
    rows: list[MessageLogEntry],
) -> str:
    lines = [
        "[conversation-compact-mechanical-fallback]",
        "- schema_version: conversation-compact-mechanical-fallback.v1",
        "- reason: compact provider completed without summary text",
        "- authority: non-authoritative conversation continuation; structured operation evidence and raw transcript remain authoritative",
    ]
    prior = " ".join(str(previous_summary or "").split())
    if prior:
        lines.append(f"- previous_summary: {_clip_compact_field(prior, 3_000)}")
    evidence = json.dumps(
        operation_evidence,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    lines.append(f"- operation_evidence: {_clip_compact_field(evidence, 3_000)}")
    lines.append("- chronological_transcript:")
    for index, row in enumerate(rows, start=1):
        content = json.dumps(
            _summary_content(row),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        lines.append(
            f"  - {index}: role={row.role} content={_clip_compact_field(content, 1_200)}"
        )
    return _bounded_compact_text(
        "\n".join(lines),
        _EMPTY_RESPONSE_FALLBACK_MAX_CHARS,
    )


# LLM: Individual fallback values are single-line bounded projections and never parsed as state.
# 函数用途: 限制机械会话摘要中的单项长度，防止一条旧消息吃掉整个续接预算。
def _clip_compact_field(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 12)].rstrip() + "...[clipped]"


# LLM: Global fallback truncation retains both the oldest task context and newest conversation
# facts, with an explicit middle omission rather than silent head-only loss.
# 函数用途: 将机械会话摘要限制在固定预算内，同时保留开头背景和末尾最新对话。
def _bounded_compact_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[conversation compact middle omitted]...\n"
    budget = max(0, limit - len(marker))
    head_chars = int(budget * 0.45)
    tail_chars = budget - head_chars
    return (
        text[:head_chars].rstrip()
        + marker
        + (text[-tail_chars:].lstrip() if tail_chars > 0 else "")
    )


# LLM: compact 可以压缩自然语言，但不能压缩掉或改写程序记录的操作终态；该账本只合并
# assistant metadata 的 operation_verification 和匹配原生工具的路径参数，不读消息正文。
# 函数用途: 合并公开操作核验与原样历史路径提示；和摘要同一次 checkpoint/CAS 提交，不依赖模型记住目录。
def _merge_compact_operation_evidence(
    previous: object,
    rows: list[MessageLogEntry],
) -> dict[str, object]:
    from ..tooling.operation_verification import public_operation_verification
    from .compact_tool_refs import merge_compact_tool_refs

    prior = (
        previous
        if isinstance(previous, dict)
        and previous.get("schema") == "conversation_operation_evidence.v1"
        else {}
    )
    assistant_message_count = _nonnegative_int(prior.get("assistant_message_count"))
    verified_assistant_message_count = _nonnegative_int(
        prior.get("verified_assistant_message_count")
    )
    operation_event_count = _nonnegative_int(prior.get("operation_event_count"))
    operation_count = _nonnegative_int(prior.get("operation_count"))
    counts = _operation_counts(prior.get("counts"))
    events = _operation_events(prior.get("events"))
    for row in rows:
        if row.role != "assistant" or is_audit_background_transcript_entry(row):
            continue
        assistant_message_count += 1
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        raw = metadata.get("operation_verification")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != "operation_verification.public.v1"
        ):
            continue
        verification = public_operation_verification(raw)
        verified_assistant_message_count += 1
        operation_count += _nonnegative_int(verification.get("operation_count"))
        current_counts = _operation_counts(verification.get("counts"))
        for key in _VERIFICATION_COUNT_KEYS:
            counts[key] += current_counts[key]
        if _nonnegative_int(verification.get("operation_count")) > 0:
            operation_event_count += 1
            events.append(
                {
                    "assistant_sequence": assistant_message_count,
                    "verification": verification,
                }
            )
    events = events[-_MAX_COMPACT_OPERATION_EVENTS:]
    unverified = max(
        0,
        assistant_message_count - verified_assistant_message_count,
    )
    return {
        "schema": "conversation_operation_evidence.v1",
        "coverage": "complete" if unverified == 0 else "partial",
        "assistant_message_count": assistant_message_count,
        "verified_assistant_message_count": verified_assistant_message_count,
        "unverified_assistant_message_count": unverified,
        "operation_event_count": operation_event_count,
        "operation_count": operation_count,
        "counts": counts,
        "omitted_event_count": max(
            0,
            operation_event_count - len(events),
        ),
        "events": events,
        "observed_tool_paths": merge_compact_tool_refs(prior.get("observed_tool_paths"), rows),
    }


# LLM: Recent raw turns keep their own structured operation projection until those turns are
# compacted; this prevents retaining prose while temporarily dropping its verification metadata.
# 函数用途: 从尚未压缩的近期 assistant metadata 提取操作核验；没有 assistant 时不注入空账本。
def _recent_operation_evidence(
    rows: list[MessageLogEntry],
) -> dict[str, object] | None:
    if not any(
        row.role == "assistant"
        and not is_audit_background_transcript_entry(row)
        and isinstance(row.metadata, dict)
        and isinstance(row.metadata.get("operation_verification"), dict)
        for row in rows
    ):
        return None
    return _merge_compact_operation_evidence({}, rows)


def _operation_events(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    events: list[dict[str, object]] = []
    for item in value[-_MAX_COMPACT_OPERATION_EVENTS:]:
        if not isinstance(item, dict):
            continue
        raw = item.get("verification")
        if not isinstance(raw, dict):
            continue
        from ..tooling.operation_verification import public_operation_verification

        events.append(
            {
                "assistant_sequence": _nonnegative_int(
                    item.get("assistant_sequence")
                ),
                "verification": public_operation_verification(raw),
            }
        )
    return events


def _operation_counts(value: object) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        key: _nonnegative_int(source.get(key))
        for key in _VERIFICATION_COUNT_KEYS
    }


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Compact input keeps prose, operation facts, and exactly one active terminal-fold projection
# in separate fields; it must never duplicate inactive hot/cold text or infer state from prose.
# 函数用途: 为摘要模型保留正文、权威操作核验和跨回合工具折叠，供真正 Compact 一次性吸收。
def _summary_content(row: MessageLogEntry) -> object:
    content = project_user_reply(row.content).content if row.role == "assistant" else row.content
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    verification = metadata.get("operation_verification")
    terminal_tool_fold = conversation_terminal_tool_fold_projection(
        metadata.get(TERMINAL_TOOL_FOLD_METADATA_KEY)
    )
    structured: dict[str, object] = {"content": content}
    if (
        row.role == "assistant"
        and isinstance(verification, dict)
        and verification.get("schema") == "operation_verification.public.v1"
    ):
        structured["operation_verification"] = verification
    if row.role == "assistant" and terminal_tool_fold:
        structured["terminal_tool_fold"] = terminal_tool_fold
    return structured if len(structured) > 1 else content


__all__ = [
    "ConversationCompactCircuitOpenError",
    "ConversationCompactError",
    "ConversationCompactResult",
    "ConversationContextUsage",
    "ConversationScope",
    "conversation_scope",
    "inspect_conversation_context",
    "prepare_conversation_context",
    "render_conversation_context_usage",
]
