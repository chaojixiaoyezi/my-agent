# LLM: This module writes append-only, owner-scoped compact candidates before the live
# ConversationThread pointer is advanced. The thread's compact_checkpoint_id is the commit
# authority; an unreferenced checkpoint row is only an orphaned candidate, never live state.
# 模块用途: 保存每一代会话压缩的完整恢复快照；先落快照、再由 thread 指针确认提交，避免坏摘要先推进游标。

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..io.jsonl import append_jsonl
from .models import ConversationThread, MessageLogEntry

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent


# LLM: This immutable request keeps all fields for one checkpoint generation aligned.
# 类用途: 汇总一次已验证候选的摘要、来源、近期尾部和 token 口径，避免写快照时参数错配。
@dataclass(frozen=True)
class CompactCheckpointRequest:
    thread: ConversationThread
    summary: str
    operation_evidence: dict[str, object]
    compact_rows: tuple[MessageLogEntry, ...]
    retained_tail: tuple[MessageLogEntry, ...]
    source_end_byte_offset: int
    projected_tokens_before: int
    projected_tokens_after: int
    policy: RuntimeCompactPolicy
    forced: bool


# LLM: The returned id is content-addressed across one intended generation so retries cannot
# create competing identities for the same candidate.
# 函数用途: 根据 thread、目标代次、摘要和消息边界生成稳定 checkpoint 编号。
def compact_checkpoint_id(
    thread: ConversationThread,
    *,
    summary: str,
    source_end_message_id: str,
) -> str:
    payload = "\0".join(
        [
            thread.thread_id,
            str(thread.compact_generation + 1),
            str(source_end_message_id),
            str(summary),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"compact-{thread.compact_generation + 1}-{digest}"


# LLM: Persist the full validated candidate before ConversationStore advances the live pointer.
# The raw transcript remains authoritative and is referenced by exact byte/message boundaries.
# 函数用途: 写入一代完整压缩快照，包含摘要正文、来源边界、近期尾部和压缩前后 token，供恢复与审计。
def write_compact_checkpoint(
    agent: SimpleAgent,
    request: CompactCheckpointRequest,
) -> str:
    thread = request.thread
    summary = request.summary
    compact_rows = request.compact_rows
    retained_tail = request.retained_tail
    if not compact_rows:
        raise ValueError("conversation compact checkpoint requires source messages")
    home = getattr(agent, "home_paths", None)
    raw_root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not raw_root:
        raise OSError("owner compact directory is unavailable")
    checkpoint_id = compact_checkpoint_id(
        thread,
        summary=summary,
        source_end_message_id=compact_rows[-1].message_id,
    )
    summary_digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    backend = getattr(agent, "backend", None)
    append_jsonl(
        Path(raw_root) / "conversations" / f"{thread.thread_id}.jsonl",
        {
            "schema": "conversation_compact_checkpoint.v1",
            "event": "conversation_compact_checkpoint",
            "status": "validated_candidate",
            "commit_authority": "conversation_thread.compact_checkpoint_id",
            "checkpoint_id": checkpoint_id,
            "previous_checkpoint_id": thread.compact_checkpoint_id,
            "thread_id": thread.thread_id,
            "previous_generation": thread.compact_generation,
            "generation": thread.compact_generation + 1,
            "source_start_message_id": compact_rows[0].message_id,
            "source_start_byte_offset": thread.compacted_through_byte_offset,
            "source_end_message_id": compact_rows[-1].message_id,
            "source_end_byte_offset": max(0, int(request.source_end_byte_offset)),
            "source_messages": len(compact_rows),
            "source_messages_total": thread.compact_source_messages + len(compact_rows),
            "retained_tail_start_message_id": (
                retained_tail[0].message_id if retained_tail else ""
            ),
            "retained_tail_end_message_id": (
                retained_tail[-1].message_id if retained_tail else ""
            ),
            "retained_tail_message_ids": [row.message_id for row in retained_tail],
            "retained_tail_messages": len(retained_tail),
            "projected_tokens_before": max(0, int(request.projected_tokens_before)),
            "projected_tokens_after": max(0, int(request.projected_tokens_after)),
            "context_window_tokens": request.policy.context_window_tokens,
            "trigger_percent": request.policy.trigger_percent,
            "trigger_tokens": request.policy.trigger_tokens,
            "forced": bool(request.forced),
            "summary": summary,
            "summary_sha256": summary_digest,
            "operation_evidence": json.loads(
                json.dumps(request.operation_evidence, ensure_ascii=False)
            ),
            "backend": str(getattr(backend, "name", "") or ""),
            "model": str(getattr(backend, "model_name", "") or ""),
            "created_at": time.time(),
        },
        sort_keys=True,
    )
    return checkpoint_id


__all__ = [
    "CompactCheckpointRequest",
    "compact_checkpoint_id",
    "write_compact_checkpoint",
]
