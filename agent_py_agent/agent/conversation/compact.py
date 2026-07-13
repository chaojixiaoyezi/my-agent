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
    policy = runtime_compact_policy(agent)
    current = thread
    compacted = False
    for _attempt in range(8):
        projected = _projected_context_tokens(agent, current.summary, pending, current_prompt)
        if projected < policy.trigger_tokens:
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
        compact_rows, retained = _split_for_compact(agent, pending)
        summary = _summarize(agent, current.summary, compact_rows)
        byte_offset = store.message_byte_offset_after(
            current.thread_id,
            compact_rows[-1].message_id,
        )
        updated = store.update_compact_state(
            current.thread_id,
            summary=summary,
            compacted_through_message_id=compact_rows[-1].message_id,
            compacted_through_byte_offset=byte_offset,
            source_messages=current.compact_source_messages + len(compact_rows),
            expected_generation=current.compact_generation,
        )
        _record_compact_event(agent, updated, compact_rows, projected, policy.trigger_tokens)
        current = updated
        pending = retained
        compacted = True
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


def _projected_context_tokens(
    agent: SimpleAgent,
    summary: str,
    rows: list[MessageLogEntry],
    current_prompt: str,
) -> int:
    try:
        base = agent.prompts.build(current_prompt, [], inject=[])
    except Exception:
        base = current_prompt
    prompt_tokens = estimate_tokens(
        {
            "base_prompt": base,
            "conversation_summary": summary,
            "conversation_messages": [
                {"role": row.role, "content": row.content} for row in rows
            ],
        }
    )
    return prompt_tokens + max(0, int(getattr(agent.config, "max_tokens", 0) or 0))


def _split_for_compact(
    agent: SimpleAgent,
    rows: list[MessageLogEntry],
) -> tuple[list[MessageLogEntry], list[MessageLogEntry]]:
    configured_turns = max(
        1,
        int(getattr(agent.config, "conversation_history_max_turns", 20) or 20),
    )
    # Keep a conversational tail, but always release at least half when the
    # context is already under pressure. This also handles a few very large turns.
    if len(rows) == 1:
        return rows, []
    retain_count = min(configured_turns * 2, max(1, len(rows) // 2))
    cutoff = len(rows) - retain_count
    return rows[:cutoff], rows[cutoff:]


def _summarize(
    agent: SimpleAgent,
    previous_summary: str,
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
            "Do not invent facts, instructions, tool results, or long-term memories.",
            "Return only the updated summary in the user's language.",
            "",
            "Previous summary:",
            previous_summary or "(none)",
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


def _summary_content(row: MessageLogEntry) -> str:
    return project_user_reply(row.content).content if row.role == "assistant" else row.content


def _record_compact_event(
    agent: SimpleAgent,
    thread: ConversationThread,
    rows: list[MessageLogEntry],
    projected_tokens: int,
    trigger_tokens: int,
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
