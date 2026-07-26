from __future__ import annotations

"""Per-thread conversation compaction over the owner-scoped transcript."""

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..io.jsonl import append_jsonl
from ..memory_archive import estimate_tokens
from .channels import project_user_reply
from .models import ConversationThread, MessageLogEntry

if TYPE_CHECKING:
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


@dataclass(frozen=True)
class ConversationScope:
    owner_id: str
    owner_home: str
    thread_id: str
    canonical_user_id: str
    channel: str
    channel_conversation_id: str
    channel_user_id: str


@dataclass(frozen=True)
class ConversationCompactResult:
    thread: ConversationThread
    messages: tuple[MessageLogEntry, ...]
    projected_tokens: int
    trigger_tokens: int
    compacted: bool = False


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
    compacted = False
    force_once = bool(force)
    for _attempt in range(8):
        projected = _projected_context_tokens(
            agent,
            current.summary,
            pending,
            current_prompt,
            operation_evidence=current.compact_operation_evidence,
        )
        if projected < policy.trigger_tokens and not force_once:
            return ConversationCompactResult(
                thread=current,
                messages=tuple(pending),
                projected_tokens=projected,
                trigger_tokens=policy.trigger_tokens,
                compacted=compacted,
            )
        if not pending:
            raise RuntimeError(
                "conversation summary alone exceeds the configured compact threshold"
            )
        # 会话运行时 compaction replaces the complete history before the active
        # turn with one summary item. Keeping a percentage of the pressured tail
        # can immediately overflow again when the newest completed turn is the
        # largest one, causing duplicate summary calls without preserving more
        # authoritative data (the raw transcript remains on disk either way).
        compact_rows = pending
        operation_evidence = _merge_compact_operation_evidence(
            current.compact_operation_evidence,
            compact_rows,
        )
        summary = _summarize(
            agent,
            current.summary,
            operation_evidence,
            compact_rows,
        )
        byte_offset = store.message_byte_offset_after(
            current.thread_id,
            compact_rows[-1].message_id,
        )
        updated = store.update_compact_state(
            current.thread_id,
            summary=summary,
            operation_evidence=operation_evidence,
            compacted_through_message_id=compact_rows[-1].message_id,
            compacted_through_byte_offset=byte_offset,
            source_messages=current.compact_source_messages + len(compact_rows),
            expected_generation=current.compact_generation,
        )
        _record_compact_event(
            agent,
            updated,
            compact_rows,
            projected,
            policy.trigger_tokens,
            forced=force_once,
        )
        current = updated
        pending = []
        compacted = True
        force_once = False
    raise RuntimeError("conversation compact did not reduce context below the threshold")


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
) -> int:
    try:
        base = agent.prompts.build(current_prompt, [], inject=[])
    except Exception:
        base = current_prompt
    return estimate_tokens(
        {
            "base_prompt": base,
            "conversation_summary": summary,
            "conversation_operation_evidence": operation_evidence or {},
            "conversation_messages": [{"role": row.role, "content": row.content} for row in rows],
        }
    )


def _summarize(
    agent: SimpleAgent,
    previous_summary: str,
    operation_evidence: dict[str, object],
    rows: list[MessageLogEntry],
) -> str:
    transcript = "\n".join(
        f"{row.role}: {json.dumps(_summary_content(row), ensure_ascii=False)}" for row in rows
    )
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
        if row.role != "assistant":
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


def _record_compact_event(
    agent: SimpleAgent,
    thread: ConversationThread,
    rows: list[MessageLogEntry],
    projected_tokens: int,
    trigger_tokens: int,
    *,
    forced: bool = False,
) -> None:
    home = getattr(agent, "home_paths", None)
    raw_root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not raw_root:
        return
    root = Path(raw_root)
    digest = hashlib.sha256(thread.summary.encode("utf-8")).hexdigest()
    append_jsonl(
        root / "conversations" / f"{thread.thread_id}.jsonl",
        {
            "event": "conversation_compacted",
            "thread_id": thread.thread_id,
            "generation": thread.compact_generation,
            "compacted_through_message_id": thread.compacted_through_message_id,
            "compacted_through_byte_offset": thread.compacted_through_byte_offset,
            "source_messages": len(rows),
            "source_messages_total": thread.compact_source_messages,
            "projected_tokens": projected_tokens,
            "trigger_tokens": trigger_tokens,
            "forced": bool(forced),
            "summary_sha256": digest,
            "created_at": time.time(),
        },
        sort_keys=True,
    )


__all__ = [
    "ConversationCompactResult",
    "ConversationScope",
    "conversation_scope",
    "prepare_conversation_context",
]
