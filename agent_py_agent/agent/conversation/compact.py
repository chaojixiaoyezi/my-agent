# LLM: This is the single automatic per-thread compaction path. Raw transcript, structured
# operation evidence, recent tail, validated checkpoint, and live cursor must remain distinct.
# 模块用途: 在 owner 隔离的唯一对话历史上做自动压缩；坏摘要不得推进游标，近期完整对话仍保留原文。

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..memory_archive import estimate_tokens
from .channels import project_user_reply
from .compact_checkpoint import CompactCheckpointRequest, write_compact_checkpoint
from .compact_guard import (
    ConversationCompactCircuitOpenError,
    ConversationCompactError,
    compact_circuit_is_open,
    compact_exception_code,
    compact_partitions,
    record_compact_failure,
)
from .models import (
    ConversationCompactCommit,
    ConversationThread,
    MessageLogEntry,
    is_audit_background_transcript_entry,
)

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent
    from .store import ConversationStore

_MAX_COMPACT_OPERATION_EVENTS = 32
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


# LLM: This immutable request keeps one compact invocation's authority and token baseline aligned.
# 类用途: 将一次压缩所需的 agent、thread、原文尾部和策略打包，供候选生成与提交共用。
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


# LLM: A candidate is still non-authoritative until its checkpoint id is written and referenced.
# 类用途: 保存一个已经生成并量过大小、但尚未推进 thread 游标的摘要候选。
@dataclass(frozen=True)
class _CompactCandidate:
    summary: str
    operation_evidence: dict[str, object]
    compact_rows: tuple[MessageLogEntry, ...]
    retained_tail: tuple[MessageLogEntry, ...]
    projected_tokens_after: int


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
# the live thread pointer. At most one generation can be committed per invocation.
# 函数用途: 加载未压缩历史，必要时保留近期完整对话、生成摘要，确认压缩有效后再一次提交。
def prepare_conversation_context(
    agent: SimpleAgent,
    store: ConversationStore,
    thread: ConversationThread,
    *,
    current_prompt: str,
    exclude_request_id: str = "",
    force: bool = False,
) -> ConversationCompactResult:
    """Load the uncompacted tail and compact it before it crosses the runtime policy."""
    from ..agent_core.runtime.context_compactor import runtime_compact_policy

    rows, errors = store.messages_after_compact_report(thread)
    if errors:
        raise OSError("conversation transcript could not be read reliably")
    pending = (
        rows
        if thread.compacted_through_byte_offset > 0
        else _messages_after_cursor(rows, thread.compacted_through_message_id)
    )
    # A gateway retry happens after the current user message was durably appended.
    # It is already represented by ``current_prompt`` and must remain outside the
    # prefix being summarized, exactly like 会话运行时 keeps the active turn input while
    # replacing older history with one compact item.
    pending = _without_current_request_suffix(pending, exclude_request_id)
    policy = runtime_compact_policy(agent)
    current = thread
    attempted_at = time.time()
    projected = _projected_context_tokens(
        agent,
        current.summary,
        pending,
        current_prompt,
        operation_evidence=current.compact_operation_evidence,
        recent_operation_evidence=_recent_operation_evidence(pending),
    )
    if projected < policy.trigger_tokens and not force:
        return ConversationCompactResult(
            thread=current,
            messages=tuple(pending),
            projected_tokens=projected,
            trigger_tokens=policy.trigger_tokens,
            compacted=False,
            recent_operation_evidence=_recent_operation_evidence(pending),
        )
    if not pending and force:
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

    return _compact_pending(
        _CompactRunRequest(
            agent=agent,
            store=store,
            thread=current,
            current_prompt=current_prompt,
            pending=tuple(pending),
            policy=policy,
            projected_tokens=projected,
            forced=bool(force),
            attempted_at=attempted_at,
        )
    )


# LLM: Candidate partitions are tried without state mutation; only a candidate below the exact
# trigger reaches the commit helper.
# 函数用途: 依次尝试“保留近期完整尾部”和“无尾部”候选，找到可用候选后提交一次。
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
    for compact_rows, retained_tail in partitions:
        try:
            candidate = _build_compact_candidate(
                request,
                compact_rows,
                retained_tail,
            )
        except Exception as exc:
            record_compact_failure(
                request.store,
                request.thread,
                code=compact_exception_code(exc),
                now=request.attempted_at,
            )
            raise
        if candidate.projected_tokens_after >= request.policy.trigger_tokens:
            continue
        try:
            return _commit_compact_candidate(request, candidate)
        except Exception as exc:
            record_compact_failure(
                request.store,
                request.thread,
                code=compact_exception_code(exc),
                now=request.attempted_at,
            )
            raise

    error = ConversationCompactError(
        "conversation compact candidate did not fit below the configured threshold",
        code="COMPACT_CANDIDATE_TOO_LARGE",
    )
    record_compact_failure(
        request.store,
        request.thread,
        code=error.code,
        now=request.attempted_at,
    )
    raise error


# LLM: This helper may call the model but cannot write any state.
# 函数用途: 为指定旧段生成摘要候选，并按完整下一轮输入重新计算压缩后 token。
def _build_compact_candidate(
    request: _CompactRunRequest,
    compact_rows: list[MessageLogEntry],
    retained_tail: list[MessageLogEntry],
) -> _CompactCandidate:
    evidence = _merge_compact_operation_evidence(
        request.thread.compact_operation_evidence,
        compact_rows,
    )
    summary = _summarize(
        request.agent,
        request.thread.summary,
        evidence,
        compact_rows,
    )
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
    last_row = candidate.compact_rows[-1]
    byte_offset = request.store.message_byte_offset_after(
        request.thread.thread_id,
        last_row.message_id,
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
    updated = request.store.update_compact_state(
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


# LLM: Exclusion is keyed only by the structured gateway request id.
# 函数用途: 排除已经落盘但仍由 current_prompt 单独携带的当前请求，避免重复进入摘要。
def _without_current_request_suffix(
    rows: list[MessageLogEntry],
    request_id: str,
) -> list[MessageLogEntry]:
    """Exclude only the current request's uncommitted tail from compact input."""
    expected = str(request_id or "").strip()
    if not expected:
        return rows
    end = len(rows)
    while end > 0:
        metadata = rows[end - 1].metadata
        current = str(metadata.get("gateway_request_id") or "") if isinstance(metadata, dict) else ""
        if current != expected:
            break
        end -= 1
    return rows[:end]


# LLM: 投影只统计下一次请求实际会携带的 system/persona、summary、消息尾和当前输入；不得加入尚未生成的未来输出预算。
# 函数用途: 估算当前会话送进模型的输入 token，用于与唯一 compact 阈值比较。
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
    return estimate_tokens(
        {
            "base_prompt": base,
            "conversation_summary": summary,
            "conversation_operation_evidence": operation_evidence or {},
            "conversation_recent_operation_evidence": (
                recent_operation_evidence or {}
            ),
            "conversation_messages": [
                {"role": row.role, "content": row.content} for row in foreground_rows
            ],
        }
    )


# LLM: Summary prose is soft context; structured operation evidence remains separate authority.
# 函数用途: 让当前模型把旧摘要和新旧段合并为一份可读摘要，空结果直接失败。
def _summarize(
    agent: SimpleAgent,
    previous_summary: str,
    operation_evidence: dict[str, object],
    rows: list[MessageLogEntry],
) -> str:
    foreground_rows = [
        row for row in rows if not is_audit_background_transcript_entry(row)
    ]
    transcript = "\n".join(
        f"{row.role}: {json.dumps(_summary_content(row), ensure_ascii=False)}"
        for row in foreground_rows
    )
    if not transcript:
        transcript = "[No foreground conversation rows in this compact segment.]"
    prompt = "\n".join(
        [
            "You maintain a conversation summary for one user and one conversation thread.",
            "Summarize only the supplied facts. Preserve user preferences, decisions, named entities,",
            "unfinished work, promises, important references, and what has already been completed.",
            "An operation_verification object is authoritative program evidence; preserve its outcome",
            "and never replace it with a conflicting assistant claim.",
            "Do not invent facts, instructions, tool results, or long-term memories.",
            "Return only the updated summary in the user's language.",
            "",
            "Previous summary:",
            previous_summary or "(none)",
            "",
            "Program operation evidence for the compacted history (authoritative JSON):",
            json.dumps(operation_evidence, ensure_ascii=False, sort_keys=True),
            "",
            "New transcript segment:",
            transcript,
        ]
    )
    response = agent.backend.generate(prompt)
    summary = str(getattr(response, "text", "") or "").strip()
    if not summary:
        raise RuntimeError("conversation compact backend returned an empty summary")
    return summary


# LLM: compact 可以压缩自然语言，但不能压缩掉或改写程序记录的操作终态；该账本只合并
# assistant message metadata 中已经公开化的 operation_verification，不读消息正文。
# 函数用途: 将历次 compact 的公开操作核验与本次被压缩消息原子合并成有界证据链。
def _merge_compact_operation_evidence(
    previous: object,
    rows: list[MessageLogEntry],
) -> dict[str, object]:
    from ..tooling.operation_verification import public_operation_verification

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


# LLM: compact 输入把用户正文和程序核验事实分栏；不得从中文核验尾注反向解析执行状态。
# 函数用途: 为摘要模型保留可读正文，并在存在时附上权威公开操作核验 metadata。
def _summary_content(row: MessageLogEntry) -> object:
    content = project_user_reply(row.content).content if row.role == "assistant" else row.content
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    verification = metadata.get("operation_verification")
    if (
        row.role == "assistant"
        and isinstance(verification, dict)
        and verification.get("schema") == "operation_verification.public.v1"
    ):
        return {
            "content": content,
            "operation_verification": verification,
        }
    return content


__all__ = [
    "ConversationCompactCircuitOpenError",
    "ConversationCompactError",
    "ConversationCompactResult",
    "ConversationScope",
    "conversation_scope",
    "prepare_conversation_context",
]
