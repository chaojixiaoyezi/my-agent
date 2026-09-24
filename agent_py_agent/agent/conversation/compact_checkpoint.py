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
from ..tooling.call_ref import ToolCallRef
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


# LLM: This request records a mid-turn native IR replacement in the same owner/thread ledger.
# 类用途: 保存运行中压缩候选；裸编号用于审计，逐调用引用才是恢复过滤权威。
@dataclass(frozen=True)
class LiveToolCompactCheckpointRequest:
    thread: ConversationThread
    summary: str
    source_tool_call_ids: tuple[str, ...]
    retained_tool_call_ids: tuple[str, ...]
    projected_tokens_before: int
    projected_tokens_after: int
    policy: RuntimeCompactPolicy
    request_id: str
    attempt_id: str
    source_tool_call_refs: tuple[ToolCallRef, ...]
    retained_tool_call_refs: tuple[ToolCallRef | None, ...]
    forced: bool = False


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


# LLM: 无 refs 的旧调用保持原 ID；新候选显式把规范来源和尾部 refs 加入内容地址，避免同裸编号的
# 迟到候选在 CAS 失败后仍以同 ID 遮蔽已提交行。旧持久 ID 不重算。
# 函数用途: 为一次工具历史压缩生成稳定编号，并隔离不同原始来源的竞争候选。
def live_tool_compact_checkpoint_id(
    thread: ConversationThread,
    *,
    summary: str,
    source_tool_call_ids: tuple[str, ...],
    attempt_id: str,
    source_tool_call_refs: tuple[ToolCallRef, ...] | None = None,
    retained_tool_call_refs: tuple[ToolCallRef | None, ...] = (),
) -> str:
    payload = "\0".join(
        [
            thread.thread_id,
            str(thread.compact_generation + 1),
            "live_tool_ir",
            str(attempt_id or ""),
            *source_tool_call_ids,
            str(summary),
        ]
    )
    if source_tool_call_refs is not None:
        payload += "\0" + json.dumps(
            {
                "source_tool_call_refs": [ref.to_dict() for ref in source_tool_call_refs],
                "retained_tool_call_refs": [
                    ref.to_dict() if ref is not None else None for ref in retained_tool_call_refs
                ],
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
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
            "source_tool_pairs": 0,
            "source_tool_pairs_total": thread.compact_source_tool_pairs,
            "source_kind": "transcript",
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
            "recovery_target_tokens": request.policy.recovery_target_tokens,
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


# LLM: 写账前验证逐调用来源；保留 v2 顶层提交者和游标语义，新 refs 显式参与新候选 ID。
# 函数用途: 把当前回合被替换的工具往返先写入 owner Compact 账本，成功后才允许 thread 提交。
def write_live_tool_compact_checkpoint(
    agent: SimpleAgent,
    request: LiveToolCompactCheckpointRequest,
) -> str:
    thread = request.thread
    summary = str(request.summary or "").strip()
    source_ids = request.source_tool_call_ids
    retained_ids = request.retained_tool_call_ids
    validate_live_tool_call_boundary(
        source_ids, retained_ids,
        request.source_tool_call_refs, request.retained_tool_call_refs,
    )
    if not source_ids:
        raise ValueError("live tool compact checkpoint requires source tool pairs")
    if not summary:
        raise ValueError("live tool compact checkpoint requires a summary")
    home = getattr(agent, "home_paths", None)
    raw_root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not raw_root:
        raise OSError("owner compact directory is unavailable")
    checkpoint_id = live_tool_compact_checkpoint_id(
        thread,
        summary=summary,
        source_tool_call_ids=source_ids,
        attempt_id=request.attempt_id,
        source_tool_call_refs=request.source_tool_call_refs,
        retained_tool_call_refs=request.retained_tool_call_refs,
    )
    backend = getattr(agent, "backend", None)
    append_jsonl(
        Path(raw_root) / "conversations" / f"{thread.thread_id}.jsonl",
        {
            "schema": "conversation_compact_checkpoint.v2",
            "event": "conversation_compact_checkpoint",
            "status": "validated_candidate",
            "commit_authority": "conversation_thread.compact_checkpoint_id",
            "checkpoint_id": checkpoint_id,
            "previous_checkpoint_id": thread.compact_checkpoint_id,
            "thread_id": thread.thread_id,
            "previous_generation": thread.compact_generation,
            "generation": thread.compact_generation + 1,
            "source_kind": "live_tool_ir",
            "source_start_message_id": "",
            "source_start_byte_offset": thread.compacted_through_byte_offset,
            "source_end_message_id": "",
            "source_end_byte_offset": thread.compacted_through_byte_offset,
            "source_messages": 0,
            "source_messages_total": thread.compact_source_messages,
            "source_tool_call_ids": list(source_ids),
            "source_tool_call_refs": [ref.to_dict() for ref in request.source_tool_call_refs],
            "source_tool_pairs": len(source_ids),
            "source_tool_pairs_total": (
                thread.compact_source_tool_pairs + len(source_ids)
            ),
            "retained_tool_call_ids": list(retained_ids),
            "retained_tool_call_refs": [
                ref.to_dict() if ref is not None else None for ref in request.retained_tool_call_refs
            ],
            "retained_tool_pairs": len(retained_ids),
            "projected_tokens_before": max(
                0,
                int(request.projected_tokens_before),
            ),
            "projected_tokens_after": max(
                0,
                int(request.projected_tokens_after),
            ),
            "context_window_tokens": request.policy.context_window_tokens,
            "trigger_percent": request.policy.trigger_percent,
            "trigger_tokens": request.policy.trigger_tokens,
            "recovery_target_tokens": request.policy.recovery_target_tokens,
            "forced": bool(request.forced),
            "request_id": str(request.request_id or ""),
            "attempt_id": str(request.attempt_id or ""),
            "summary": summary,
            "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            "operation_evidence": json.loads(
                json.dumps(thread.compact_operation_evidence, ensure_ascii=False)
            ),
            "backend": str(getattr(backend, "name", "") or ""),
            "model": str(getattr(backend, "model_name", "") or ""),
            "created_at": time.time(),
        },
        sort_keys=True,
    )
    return checkpoint_id


# LLM: Only the chain ending at ConversationThread.compact_checkpoint_id is committed. Readers
# must follow previous_checkpoint_id backwards and reject a missing/corrupt link instead of treating
# an orphan candidate as authority or replaying already-compacted active-turn effects.
# 函数用途: 读取当前 thread 真正提交过的 Compact 快照链，供恢复时识别已被摘要替代的工具调用。
def committed_compact_checkpoint_chain(
    agent: SimpleAgent,
    thread: ConversationThread,
) -> tuple[dict[str, object], ...]:
    checkpoint_id = str(thread.compact_checkpoint_id or "").strip()
    generation = max(0, int(thread.compact_generation or 0))
    if not checkpoint_id and generation == 0:
        return ()
    if not checkpoint_id or generation <= 0:
        raise OSError("conversation compact pointer is incomplete")
    home = getattr(agent, "home_paths", None)
    raw_root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not raw_root:
        raise OSError("owner compact directory is unavailable")
    from ..common.json_io import read_jsonl_objects_report

    path = Path(raw_root) / "conversations" / f"{thread.thread_id}.jsonl"
    report = read_jsonl_objects_report(
        path,
        context="conversation.compact_checkpoint_chain",
    )
    if report.load_errors:
        raise OSError("conversation compact checkpoint ledger is unreadable")
    by_id = {
        str(row.get("checkpoint_id") or "").strip(): row
        for row in report.records
        if isinstance(row, dict) and str(row.get("checkpoint_id") or "").strip()
    }
    chain: list[dict[str, object]] = []
    visited: set[str] = set()
    current_id = checkpoint_id
    expected_generation = generation
    while current_id:
        if current_id in visited:
            raise OSError("conversation compact checkpoint chain contains a cycle")
        visited.add(current_id)
        row = by_id.get(current_id)
        if row is None:
            raise OSError("conversation compact checkpoint chain is incomplete")
        if str(row.get("thread_id") or "").strip() != thread.thread_id:
            raise OSError("conversation compact checkpoint thread identity mismatches")
        try:
            row_generation = int(row.get("generation") or 0)
        except (TypeError, ValueError) as exc:
            raise OSError("conversation compact checkpoint generation is invalid") from exc
        if row_generation != expected_generation:
            raise OSError("conversation compact checkpoint generation chain mismatches")
        chain.append(dict(row))
        current_id = str(row.get("previous_checkpoint_id") or "").strip()
        expected_generation -= 1
    if expected_generation != 0:
        raise OSError("conversation compact checkpoint chain ended early")
    chain.reverse()
    return tuple(chain)


# LLM: 引用数组与审计编号逐位对应；来源必须明确，未知保留项用 None 表示，绝不能删除。
# 函数用途: 校验一次压缩的来源和尾部身份边界，计数按实际记录保留，不按裸编号去重。
def validate_live_tool_call_boundary(
    source_ids: tuple[str, ...],
    retained_ids: tuple[str, ...],
    source_refs: tuple[ToolCallRef, ...],
    retained_refs: tuple[ToolCallRef | None, ...],
) -> None:
    if not source_ids or len(source_ids) != len(source_refs) or len(retained_ids) != len(retained_refs):
        raise ValueError("live tool compact ref boundary lengths are invalid")
    for ids, refs, allow_unknown in (
        (source_ids, source_refs, False), (retained_ids, retained_refs, True),
    ):
        for call_id, ref in zip(ids, refs):
            if ref is None and allow_unknown and isinstance(call_id, str):
                continue
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError("live tool compact call id is invalid")
            if not isinstance(ref, ToolCallRef) or ref.call_id != call_id:
                raise ValueError("live tool compact source ref is missing or mismatches call id")
        known = [ref for ref in refs if ref is not None]
        if len(known) != len(set(known)):
            raise ValueError("live tool compact contains duplicate exact refs")
    if set(source_refs) & set(retained_refs):
        raise ValueError("live tool compact exact source and retained refs overlap")


# LLM: 旧 checkpoint 或无效 refs 仅提供不确定性事实；只有完整、已提交的引用可以隐藏记录。
# 类用途: 区分已确认的精确来源与仍不能确定归属的旧裸编号。
@dataclass(frozen=True)
class CommittedLiveToolSources:
    refs: frozenset[ToolCallRef]
    uncertain_call_ids: frozenset[str]
    uncertain_checkpoint_ids: tuple[str, ...]


# LLM: 只读取 thread 指针确认的 live_tool_ir 链；不读取 orphan、不把顶层提交者套给来源。
# 函数用途: 提取已提交压缩的精确来源，旧行或无效扩展明确返回来源不确定。
def committed_live_tool_compact_sources(
    agent: SimpleAgent,
    thread: ConversationThread,
) -> CommittedLiveToolSources:
    refs: set[ToolCallRef] = set()
    uncertain_ids: set[str] = set()
    uncertain_checkpoints: list[str] = []
    for row in committed_compact_checkpoint_chain(agent, thread):
        if row.get("source_kind") != "live_tool_ir":
            continue
        try:
            if any(not isinstance(row.get(key), list) for key in (
                "source_tool_call_ids", "retained_tool_call_ids",
                "source_tool_call_refs", "retained_tool_call_refs",
            )):
                raise ValueError("live tool compact ref arrays are missing or invalid")
            source_refs = tuple(ToolCallRef.from_dict(value) for value in row["source_tool_call_refs"])
            retained_refs = tuple(
                ToolCallRef.from_dict(value) if value is not None else None
                for value in row["retained_tool_call_refs"]
            )
            validate_live_tool_call_boundary(
                tuple(row["source_tool_call_ids"]), tuple(row["retained_tool_call_ids"]),
                source_refs, retained_refs,
            )
        except (KeyError, TypeError, ValueError):
            uncertain_checkpoints.append(str(row["checkpoint_id"]))
            values = row.get("source_tool_call_ids")
            if isinstance(values, list):
                uncertain_ids.update(value for value in values if isinstance(value, str))
            continue
        refs.update(source_refs)
    return CommittedLiveToolSources(frozenset(refs), frozenset(uncertain_ids), tuple(uncertain_checkpoints))


__all__ = [
    "CompactCheckpointRequest",
    "LiveToolCompactCheckpointRequest",
    "compact_checkpoint_id",
    "committed_compact_checkpoint_chain",
    "committed_live_tool_compact_sources",
    "live_tool_compact_checkpoint_id",
    "write_live_tool_compact_checkpoint",
    "write_compact_checkpoint",
    "validate_live_tool_call_boundary",
]
