# LLM: v3候选在原owner账本追加，thread head/CAS仍是唯一提交权威；scope/base/精确覆盖与摘要一起封印，
# v1/v2仅通过原版本合同读取。局部摘要不能因在同一提交链就扩大适用范围，孤立候选不可用于隐藏来源。
# 模块用途: 保存双来源恢复点并由原线程CAS确认；读取按同次行地址逐行校验，旧摘要无需全驻留。

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..io.jsonl import append_jsonl
from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from .compact_tool_identity import compact_tool_refs
from .message_replay import message_rows_iterator
from .models import ConversationThread, MessageLogEntry

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent


# LLM: 同一候选的scope/base、原消息和计量必须对齐；引用是同链读取投影，不授予任务权限。
# 类用途: 汇总摘要范围、可重放来源及近期尾部，交给原检查点写入，不要求正文tuple常驻。
@dataclass(frozen=True)
class CompactCheckpointRequest:
    thread: ConversationThread
    summary: str
    operation_evidence: dict[str, object]
    compact_rows: Sequence[MessageLogEntry]
    retained_tail: Sequence[MessageLogEntry]
    source_end_byte_offset: int
    projected_tokens_before: int
    projected_tokens_after: int
    policy: RuntimeCompactPolicy
    forced: bool
    scope: CompactScope = THREAD_COMPACT_SCOPE
    summary_base_checkpoint_id: str | None = None
    source_tool_refs: tuple[dict[str, str], ...] = ()
    retained_tool_refs: tuple[dict[str, str], ...] = ()
    # LLM: 媒体策略事实只在本次覆盖范围含媒体块时写入 checkpoint；refs 为完整 sha256，供审计与用户按内容地址重新附上。
    #   archived 与 summarized 互斥：A 路径计 archived，B 路径计 summarized；reason 记录 auto 下没走 B 的结构化原因。
    media_policy: str = ""
    media_fact_source: str = ""
    media_blocks_archived: int = 0
    media_blocks_summarized: int = 0
    media_policy_reason: str = ""
    media_refs: tuple[str, ...] = ()


# LLM: 工具来源/保留区均以原四元refs为权威，call_id仅作展示；与scope/base共同写入原owner账本。
# 类用途: 保存一次活动工具压缩候选及准确来源，避免跨轮同名调用被误替代。
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
    source_tool_refs: tuple[dict[str, str], ...]
    retained_tool_refs: tuple[dict[str, str], ...] = ()
    forced: bool = False
    scope: CompactScope = THREAD_COMPACT_SCOPE
    summary_base_checkpoint_id: str | None = None


# LLM: v3身份绑定提交前驱、摘要基础、scope、精确覆盖及创建时间；时间影响任务继承，必须封印。
# 重试在新时间创建的是另一候选，原CAS仍只允许一个获胜；旧v1/v2不能使用新身份算法写入。
# 函数用途: 为同一提交候选生成内容身份，防止摘要相同但适用范围不同的候选混同。
def scoped_compact_checkpoint_id(row: dict[str, object]) -> str:
    keys = ("schema", "thread_id", "generation", "previous_checkpoint_id", "summary_base_checkpoint_id",
            "scope", "source_kind", "source_message_ids", "source_tool_refs", "summary_sha256",
            "operation_evidence", "source_end_byte_offset", "retained_tail_message_ids", "retained_tool_call_ids",
            "created_at", "retained_tool_refs")
    payload = json.dumps({key: row.get(key) for key in keys}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"compact-v3-{row['generation']}-{digest}"


# LLM: 摘要基础是适用视图而不是链head；显式基础也必须是该准备代次的同一适用摘要，不能暗中跨任务继承。
# 函数用途: 校验原提交链并固定候选将要继承的摘要引用，不写持久状态。
def _summary_base(agent, request) -> str:
    from .compact_summary_view import resolve_compact_summary_view

    view = resolve_compact_summary_view(agent, request.thread, request.scope)
    requested = request.summary_base_checkpoint_id
    if requested is not None and requested != view.checkpoint_id:
        raise ValueError("compact summary base changed or is outside the requested scope")
    return view.checkpoint_id


# LLM: 所有来源共用v3头和原用量事实；source字段由各来源提供，元数据不授予运行权限。
# 函数用途: 固定检查点的同链前驱、摘要基础、范围和预算，避免两种压缩产生不同权威。
def _checkpoint_payload(agent, request) -> dict[str, object]:
    thread = request.thread
    summary = str(request.summary or "").strip()
    if not summary:
        raise ValueError("compact checkpoint requires a summary")
    backend = getattr(agent, "backend", None)
    return {
        "schema": "conversation_compact_checkpoint.v3", "event": "conversation_compact_checkpoint",
        "status": "validated_candidate", "commit_authority": "conversation_thread.compact_checkpoint_id",
        "previous_checkpoint_id": thread.compact_checkpoint_id, "thread_id": thread.thread_id,
        "previous_generation": thread.compact_generation, "generation": thread.compact_generation + 1,
        "summary_base_checkpoint_id": _summary_base(agent, request), "scope": request.scope.to_dict(),
        "source_message_ids": [], "source_tool_refs": [],
        "source_start_byte_offset": thread.compacted_through_byte_offset,
        "source_end_byte_offset": thread.compacted_through_byte_offset,
        "source_messages": 0, "source_messages_total": thread.compact_source_messages,
        "source_tool_pairs": 0, "source_tool_pairs_total": thread.compact_source_tool_pairs,
        "projected_tokens_before": max(0, int(request.projected_tokens_before)),
        "projected_tokens_after": max(0, int(request.projected_tokens_after)),
        "context_window_tokens": request.policy.context_window_tokens,
        "trigger_percent": request.policy.trigger_percent, "trigger_tokens": request.policy.trigger_tokens,
        "recovery_target_tokens": request.policy.recovery_target_tokens, "forced": bool(request.forced),
        "summary": summary, "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "backend": str(getattr(backend, "name", "") or ""),
        "model": str(getattr(backend, "model_name", "") or ""), "created_at": time.time(),
    }


# LLM: 唯一追加路径先落完整候选，head仍由原CAS发布；写失败不生成提交权，orphan不可见。
# 函数用途: 将内容寻址的候选写入原owner检查点账本，不更新线程。
def _append_checkpoint(agent, row: dict[str, object]) -> str:
    home = getattr(agent, "home_paths", None)
    raw_root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not raw_root:
        raise OSError("owner compact directory is unavailable")
    row["checkpoint_id"] = scoped_compact_checkpoint_id(row)
    append_jsonl(Path(raw_root) / "conversations" / f"{row['thread_id']}.jsonl", row, sort_keys=True)
    return row["checkpoint_id"]


# LLM: 同一候选记录双覆盖；短路校验显式关闭来源，双refs与摘要封印后只经一次CAS发布，不改live-tool写入。
# 函数用途: 从同一只读消息序列保存精确覆盖和保留IDs，持久schema及原CAS不变。
def write_compact_checkpoint(agent: SimpleAgent, request: CompactCheckpointRequest) -> str:
    compact_rows = request.compact_rows
    with message_rows_iterator(compact_rows) as rows:
        foreign_source = any(row.thread_id != request.thread.thread_id for row in rows)
    if not compact_rows or foreign_source:
        raise ValueError("conversation compact checkpoint requires source messages from its thread")
    source_ids = [item.message_id for item in compact_rows]
    if any(not isinstance(item, str) or not item.strip() for item in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("conversation compact source messages need distinct identities")
    refs = compact_tool_refs(list(request.source_tool_refs))
    retained_refs = compact_tool_refs(list(request.retained_tool_refs))
    from .compact_tool_identity import compact_tool_ref_key

    if {compact_tool_ref_key(ref) for ref in refs} & {compact_tool_ref_key(ref) for ref in retained_refs}:
        raise ValueError("compact source and retained tools overlap")
    row = _checkpoint_payload(agent, request)
    row.update({
        "source_kind": "transcript_and_tool_archive" if refs else "transcript", "source_start_message_id": compact_rows[0].message_id,
        "source_end_message_id": compact_rows[-1].message_id,
        "source_end_byte_offset": max(0, int(request.source_end_byte_offset)),
        "source_message_ids": source_ids,
        "source_messages": len(compact_rows),
        "source_tool_refs": list(refs), "source_tool_call_ids": [ref["call_id"] for ref in refs],
        "source_tool_pairs": len(refs), "source_tool_pairs_total": request.thread.compact_source_tool_pairs + len(refs),
        "retained_tool_refs": list(retained_refs), "retained_tool_call_ids": [ref["call_id"] for ref in retained_refs],
        "retained_tool_pairs": len(retained_refs),
        "source_messages_total": request.thread.compact_source_messages + len(compact_rows),
        "retained_tail_start_message_id": request.retained_tail[0].message_id if request.retained_tail else "",
        "retained_tail_end_message_id": request.retained_tail[-1].message_id if request.retained_tail else "",
        "retained_tail_message_ids": [item.message_id for item in request.retained_tail],
        "retained_tail_messages": len(request.retained_tail),
        "operation_evidence": json.loads(json.dumps(request.operation_evidence, ensure_ascii=False)),
    })
    if request.media_policy and (request.media_blocks_archived > 0 or request.media_blocks_summarized > 0):
        row.update({
            "media_policy": request.media_policy, "media_fact_source": request.media_fact_source,
            "media_blocks_archived": int(request.media_blocks_archived),
            "media_blocks_summarized": int(request.media_blocks_summarized), "media_refs": list(request.media_refs),
            **({"media_policy_reason": request.media_policy_reason} if request.media_policy_reason else {}),
        })
    return _append_checkpoint(agent, row)


# LLM: 运行中摘要覆盖原调用四元身份，不能把摘要请求的request/attempt当作被压调用来源；缺失时拒绝写候选。
# 函数用途: 保存精确工具覆盖和同一摘要基础，仍不改变原工具账或全线程游标。
def write_live_tool_compact_checkpoint(agent: SimpleAgent, request: LiveToolCompactCheckpointRequest) -> str:
    refs = compact_tool_refs(list(request.source_tool_refs))
    retained_refs = compact_tool_refs(list(request.retained_tool_refs))
    source_ids = tuple(ref["call_id"] for ref in refs)
    if not source_ids:
        raise ValueError("live tool compact checkpoint requires source tool pairs")
    if source_ids != request.source_tool_call_ids or tuple(ref["call_id"] for ref in retained_refs) != request.retained_tool_call_ids:
        raise ValueError("compact call projection does not match exact references")
    row = _checkpoint_payload(agent, request)
    base_evidence = request.thread.compact_operation_evidence
    if request.scope.kind != "thread":
        from .compact_summary_view import resolve_compact_summary_view

        base_evidence = resolve_compact_summary_view(agent, request.thread, request.scope).operation_evidence
    row.update({
        "source_kind": "live_tool_ir", "source_start_message_id": "", "source_end_message_id": "",
        "source_tool_call_ids": list(source_ids), "source_tool_refs": list(refs),
        "source_tool_pairs": len(refs), "source_tool_pairs_total": request.thread.compact_source_tool_pairs + len(refs),
        "retained_tool_call_ids": list(request.retained_tool_call_ids),
        "retained_tool_pairs": len(retained_refs), "retained_tool_refs": list(retained_refs),
        "request_id": str(request.request_id or ""), "attempt_id": str(request.attempt_id or ""),
        "operation_evidence": json.loads(json.dumps(base_evidence, ensure_ascii=False)),
    })
    return _append_checkpoint(agent, row)


# LLM: 完整chain接口保留旧顺序及行形状，校验共用唯一逆向reader；完整返回者仍承担全部正文内存。
# 函数用途: 为需要检查全部快照的调用方返回从旧到新的已提交链，不把orphan算入结果。
def committed_compact_checkpoint_chain(agent: SimpleAgent, thread: ConversationThread) -> tuple[dict[str, object], ...]:
    with closing(iter_committed_compact_checkpoints(agent, thread)) as chain:
        return tuple(reversed(tuple(chain)))


# LLM: 从原thread head逆序逐行校验后yield；消费者必须完整耗尽后才能采用，失败/提前退出须close以释放描述符。
# v3内容封印及旧版本合同不变，原writer/CAS唯一；不常驻整份账本或未提交候选正文。
# 函数用途: 为摘要视图提供最新到最旧的只读检查点，完整链不成立时拒绝恢复。
def iter_committed_compact_checkpoints(agent: SimpleAgent, thread: ConversationThread):
    checkpoint_id = str(thread.compact_checkpoint_id or "").strip()
    generation = max(0, int(thread.compact_generation or 0))
    if not checkpoint_id and generation == 0:
        return
    if not checkpoint_id or generation <= 0:
        raise OSError("conversation compact pointer is incomplete")
    home = getattr(agent, "home_paths", None)
    raw_root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not raw_root:
        raise OSError("owner compact directory is unavailable")
    from .compact_checkpoint_scan import checkpoint_row_lookup

    path = Path(raw_root) / "conversations" / f"{thread.thread_id}.jsonl"
    with checkpoint_row_lookup(path) as lookup:
        visited: set[str] = set()
        current_id = checkpoint_id
        expected_generation = generation
        while current_id:
            if current_id in visited:
                raise OSError("conversation compact checkpoint chain contains a cycle")
            visited.add(current_id)
            row = lookup(current_id)
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
            is_v3 = row.get("schema") == "conversation_compact_checkpoint.v3"
            if is_v3 != current_id.startswith("compact-v3-"):
                raise OSError("conversation compact checkpoint version identity mismatches")
            if not is_v3 and any(key in row for key in ("scope", "summary_base_checkpoint_id", "source_tool_refs", "source_message_ids")):
                raise OSError("legacy conversation compact checkpoint contains new scope fields")
            if is_v3 and scoped_compact_checkpoint_id(row) != current_id:
                raise OSError("conversation compact checkpoint content identity mismatches")
            current_id = str(row.get("previous_checkpoint_id") or "").strip()
            expected_generation -= 1
            yield row
        if expected_generation != 0:
            raise OSError("conversation compact checkpoint chain ended early")


# LLM: 覆盖来自实际适用摘要的语义基础链，不能把同提交链其它任务的来源一起隐藏。
# 函数用途: 返回全线程摘要真正覆盖的工具引用；局部宿主应传入同次已应用的摘要视图。
def committed_live_tool_compact_source_refs(agent: SimpleAgent, thread: ConversationThread) -> tuple[dict[str, object], ...]:
    from .compact_summary_view import resolve_compact_summary_view

    return resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE).source_tool_refs


__all__ = [
    "CompactCheckpointRequest",
    "LiveToolCompactCheckpointRequest",
    "scoped_compact_checkpoint_id",
    "committed_compact_checkpoint_chain",
    "committed_live_tool_compact_source_refs",
    "write_live_tool_compact_checkpoint",
    "write_compact_checkpoint",
]
