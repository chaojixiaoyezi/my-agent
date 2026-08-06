from __future__ import annotations

"""Curator 模型输出的宿主证据验证与精确游标计算。"""

# LLM: Provider Schema is only a shape gate; this module binds every claimed identity to the
# current owner input snapshot and computes cursors without trusting model suggestions.
# 模块用途: 防伪造 user/tool 引用、规范化证据，并只越过连续 processed 前缀。

import hashlib
import json
from dataclasses import dataclass, replace

from .candidate_models import (
    MemoryScope,
    normalize_iso_time,
    normalize_reference_list,
    normalize_string_list,
)
from .curator_inputs import CuratorAuditInput, CuratorInputBatch, CuratorMessageInput
from .curator_models import CURATOR_MODEL_ORIGINS, CuratorExtraction

_SUCCESS_TOOL_STATUSES = frozenset(
    {"success", "succeeded", "ok", "completed", "committed", "applied"}
)


# LLM: Provider evidence failures expose one stable persisted error code while retaining a
# non-content detail code in-process for diagnostics and tests.
# 类用途: 区分严格 JSON Schema 错误与引用、状态或覆盖证明错误。
class CuratorEvidenceError(ValueError):
    # LLM: Only the stable top-level code is printable/persistable; detail_code contains no
    # provider text and is limited to a host-authored enum-like diagnostic.
    # 函数用途: 创建不泄露正文的 Curator 证据错误。
    def __init__(self, detail_code: str) -> None:
        super().__init__("CURATOR_EVIDENCE_INVALID")
        self.detail_code = str(detail_code or "evidence_invalid")


# LLM: One immutable result keeps cursor maps and processed counts from different validation
# passes from being mixed during commit.
# 类用途: 表示宿主根据输入顺序计算出的下一游标与实际推进数量。
@dataclass(frozen=True)
class CuratorCursorAdvance:
    per_thread_cursors: dict[str, str]
    last_audit_event_id: str
    processed_messages: int
    processed_audit_events: int


# LLM: Schema-valid output still cannot grant itself owner evidence; every message/tool ref is
# replaced with a canonical input-snapshot reference.
# 函数用途: 验证 CuratorExtraction 的处理覆盖和证据归属。
def validate_extraction(
    extraction: CuratorExtraction,
    batch: CuratorInputBatch,
    *,
    owner_id: str,
) -> CuratorExtraction:
    messages = {item.message_id: item for item in batch.messages}
    audits_by_event = {item.event_id: item for item in batch.audit_events}
    audits_by_call = {
        item.tool_call_id: item for item in batch.audit_events if item.tool_call_id
    }
    audits_by_operation = {
        item.operation_id: item for item in batch.audit_events if item.operation_id
    }
    artifacts_by_ref = _artifacts_by_ref(batch)
    _validate_processed_refs(extraction, messages, audits_by_event)
    daily = [
        _validated_daily(
            draft,
            messages=messages,
            audits_by_event=audits_by_event,
            audits_by_call=audits_by_call,
            audits_by_operation=audits_by_operation,
            artifacts_by_ref=artifacts_by_ref,
            owner_id=owner_id,
        )
        for draft in extraction.daily_events
    ]
    candidates = [
        _validated_candidate(
            observation,
            messages=messages,
            audits_by_event=audits_by_event,
            audits_by_call=audits_by_call,
            audits_by_operation=audits_by_operation,
            artifacts_by_ref=artifacts_by_ref,
            owner_id=owner_id,
        )
        for observation in extraction.candidates
    ]
    return replace(extraction, daily_events=tuple(daily), candidates=tuple(candidates))


# LLM: Daily user/tool origins must carry the same canonical evidence standard as candidates;
# entering a non-authoritative daily ledger never relaxes provenance.
# 函数用途: 验证并替换一条 Daily 草稿的引用。
# LLM: Daily 工具引用只保留成功终态；失败/超时/取消/unknown 的引用剔除但不阻断整批(经历仍记 summary)。
# 函数用途: 对 Daily 草稿的 tool_refs 做容错 canonicalization，成功终态保留、非成功剔除。
def _daily_tool_refs_tolerant(
    refs: object,
    by_event: dict[str, CuratorAuditInput],
    by_call: dict[str, CuratorAuditInput],
    by_operation: dict[str, CuratorAuditInput],
    *,
    owner_id: str,
) -> list[dict[str, object]]:
    canonical: list[dict[str, object]] = []
    for ref in normalize_reference_list(refs):
        source = _tool_source(ref, by_event, by_call, by_operation)
        if source is None:
            continue  # 引用不在本批 owner 审计 → 剔除(不阻断)
        if source.status.strip().lower() not in _SUCCESS_TOOL_STATUSES:
            continue  # 失败/超时/unknown 工具 → 剔除(不阻断,经历仍进 summary)
        resolved = source.ref()
        if owner_id:
            resolved["owner_id"] = owner_id
        canonical.append(resolved)
    return canonical


def _validated_daily(
    draft: object,
    *,
    messages: dict[str, CuratorMessageInput],
    audits_by_event: dict[str, CuratorAuditInput],
    audits_by_call: dict[str, CuratorAuditInput],
    audits_by_operation: dict[str, CuratorAuditInput],
    artifacts_by_ref: dict[str, CuratorAuditInput],
    owner_id: str,
):
    _require_curator_origin(draft.origin)
    message_refs = _canonical_message_refs(draft.message_refs, messages)
    # Daily 是"经历摘要"而非"事实证据"：后台模型可能把失败/超时的工具调用误标 tool_verified
    # 写进 daily 的 tool_refs。宿主确定性容错：非成功终态的工具引用从 daily 剔除(不 raise、
    # 不阻断整批)，成功终态保留；summary 文本仍如实记录经历。这符合"失败工具不能当 verified
    # 证据、但可作经历记录"的证据边界，且不依赖模型输出质量。
    tool_refs = _daily_tool_refs_tolerant(
        draft.tool_refs,
        audits_by_event,
        audits_by_call,
        audits_by_operation,
        owner_id=owner_id,
    )
    artifact_refs = _canonical_artifact_refs(
        draft.artifact_refs,
        artifacts_by_ref,
    )
    if draft.origin == "user_explicit" and not any(
        ref.get("role") == "user" for ref in message_refs
    ):
        raise CuratorEvidenceError("daily_user_explicit_without_user_message")
    if not (message_refs or tool_refs or artifact_refs):
        raise CuratorEvidenceError("daily_without_evidence")
    references = [*message_refs, *tool_refs, *artifact_refs]
    return replace(
        draft,
        session_id=_single_reference_value(references, "session_id"),
        thread_id=_single_reference_value(references, "thread_id"),
        request_id=_single_reference_value(references, "request_id"),
        task_id=_single_reference_value(references, "task_id"),
        run_id=_single_reference_value(references, "run_id"),
        created_at=_earliest_reference_time(references),
        message_refs=tuple(message_refs),
        tool_refs=tuple(tool_refs),
        artifact_refs=tuple(artifact_refs),
    )


# LLM: Candidate evidence is canonicalized before CandidateService sees it, and explicit/tool
# origins cannot be asserted by confidence or prose alone.
# 函数用途: 验证并替换一条候选 observation 的引用。
def _validated_candidate(
    observation: object,
    *,
    messages: dict[str, CuratorMessageInput],
    audits_by_event: dict[str, CuratorAuditInput],
    audits_by_call: dict[str, CuratorAuditInput],
    audits_by_operation: dict[str, CuratorAuditInput],
    artifacts_by_ref: dict[str, CuratorAuditInput],
    owner_id: str,
):
    _require_curator_origin(observation.origin)
    message_refs = _canonical_message_refs(observation.source_message_refs, messages)
    tool_refs = _canonical_tool_refs(
        observation.source_tool_refs,
        audits_by_event,
        audits_by_call,
        audits_by_operation,
        require_success=observation.origin == "tool_verified",
        owner_id=owner_id,
    )
    artifact_refs = _canonical_artifact_refs(
        observation.source_artifact_refs,
        artifacts_by_ref,
    )
    references = [*message_refs, *tool_refs, *artifact_refs]
    task_ids = tuple(_reference_values(references, "task_id"))
    run_ids = tuple(_reference_values(references, "run_id"))
    if observation.origin == "user_explicit":
        user_refs = [ref for ref in message_refs if ref.get("role") == "user"]
        if not user_refs or not any(str(ref.get("quote") or "") for ref in user_refs):
            raise CuratorEvidenceError("candidate_user_explicit_without_quote")
    if observation.origin == "tool_verified" and not tool_refs:
        raise CuratorEvidenceError("candidate_tool_verified_without_tool")
    if not (message_refs or tool_refs or artifact_refs or task_ids or run_ids):
        raise CuratorEvidenceError("candidate_without_evidence")
    canonical_observation_id = _canonical_observation_id(
        observation,
        message_refs=message_refs,
        tool_refs=tool_refs,
        artifact_refs=artifact_refs,
        task_ids=task_ids,
        run_ids=run_ids,
    )
    canonical_evidence = normalize_reference_list(
        [
            *message_refs,
            *tool_refs,
            *artifact_refs,
            *({"ref_id": f"task:{value}", "task_id": value} for value in task_ids),
            *({"ref_id": f"run:{value}", "run_id": value} for value in run_ids),
        ]
    )
    return replace(
        observation,
        evidence_refs=tuple(canonical_evidence),
        source_message_refs=tuple(message_refs),
        source_tool_refs=tuple(tool_refs),
        source_artifact_refs=tuple(artifact_refs),
        source_task_ids=task_ids,
        source_run_ids=run_ids,
        observation_id=canonical_observation_id,
    )


# LLM: Artifact identities come only from the frozen owner audit batch; provider text cannot add
# another path or ref to this exact lookup.
# 函数用途: 构造当前批次 artifact_ref 到正式 audit 输入的映射。
def _artifacts_by_ref(batch: CuratorInputBatch) -> dict[str, CuratorAuditInput]:
    artifacts: dict[str, CuratorAuditInput] = {}
    for event in batch.audit_events:
        artifact_ref = str(event.artifact_ref or "").strip()
        if artifact_ref:
            artifacts.setdefault(artifact_ref, event)
    return artifacts


# LLM: Runtime IDs are derived only from canonicalized message/tool/artifact refs; when a Daily
# summary spans more than one identity the field stays empty instead of guessing one.
# 函数用途: 返回引用中唯一的非空结构化字段值。
def _single_reference_value(refs: list[dict[str, object]], key: str) -> str:
    values = _reference_values(refs, key)
    return values[0] if len(values) == 1 else ""


# LLM: Candidate task/run provenance follows canonical evidence order and deduplicates exact IDs;
# provider output has no source_task_ids/source_run_ids field to forge.
# 函数用途: 从一组宿主引用提取稳定非空字段列表。
def _reference_values(refs: list[dict[str, object]], key: str) -> list[str]:
    return normalize_string_list([ref.get(key) for ref in refs])


# LLM: Daily partition time is the earliest canonical evidence time, not a provider-selected day;
# input timestamps are normalized to UTC before comparison.
# 函数用途: 为一条 Daily 草稿推导稳定 created_at。
def _earliest_reference_time(refs: list[dict[str, object]]) -> str:
    values = [
        normalize_iso_time(ref.get("created_at"), allow_empty=False)
        for ref in refs
        if str(ref.get("created_at") or "").strip()
    ]
    if not values:
        raise CuratorEvidenceError("daily_evidence_without_time")
    return min(values)


# LLM: Artifact evidence is replaced with the exact audit identity and hash; a model cannot
# invent a path/ref or attach raw output fields.
# 函数用途: 将模型 artifact 引用绑定到本批 audit 中的正式 artifact_ref。
def _canonical_artifact_refs(
    refs: object,
    by_ref: dict[str, CuratorAuditInput],
) -> list[dict[str, object]]:
    canonical: list[dict[str, object]] = []
    for ref in normalize_reference_list(refs):
        artifact_ref = str(ref.get("artifact_ref") or "").strip()
        source = by_ref.get(artifact_ref)
        if source is None:
            raise CuratorEvidenceError("artifact_outside_owner_batch")
        canonical.append(
            {
                "artifact_ref": source.artifact_ref,
                "event_id": source.event_id,
                "content_hash": source.artifact_hash or source.content_hash,
                "size_bytes": source.artifact_size_bytes,
                "task_id": source.task_id,
                "run_id": source.run_id,
                "created_at": source.created_at,
            }
        )
    return canonical


# LLM: Provider observation_id is advisory only; the host hashes canonical evidence plus typed
# subject/scope/action so paraphrased replay cannot fabricate another independent occurrence.
# 函数用途: 生成一次 Curator Candidate 观察的稳定宿主身份。
def _canonical_observation_id(
    observation: object,
    *,
    message_refs: list[dict[str, object]],
    tool_refs: list[dict[str, object]],
    artifact_refs: list[dict[str, object]],
    task_ids: tuple[str, ...],
    run_ids: tuple[str, ...],
) -> str:
    material = {
        "candidate_type": str(observation.candidate_type or "").strip().lower(),
        "subject_key": str(observation.subject_key or "").strip().lower(),
        "scope": MemoryScope.from_value(observation.scope).to_dict(),
        "proposed_action": str(observation.proposed_action or "").strip().lower(),
        "target_entry_id": str(observation.target_entry_id or "").strip(),
        "promotion_target": str(observation.promotion_target or "").strip().lower(),
        "message_ids": sorted(str(ref.get("message_id") or "") for ref in message_refs),
        "tool_event_ids": sorted(str(ref.get("event_id") or "") for ref in tool_refs),
        "artifact_refs": sorted(
            str(ref.get("artifact_ref") or "") for ref in artifact_refs
        ),
        "task_ids": sorted(task_ids),
        "run_ids": sorted(run_ids),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "curator-observation-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


# LLM: The Curator cannot self-assert reviewed or subagent provenance; those origins enter only
# through their dedicated host-side CandidateService adapters.
# 函数用途: 限制后台模型可声明的来源类别。
def _require_curator_origin(origin: object) -> None:
    if str(origin or "").strip().lower() not in CURATOR_MODEL_ORIGINS:
        raise CuratorEvidenceError("host_owned_origin")


# LLM: Every input is exactly processed or unresolved; omissions and unknown IDs block cursor
# advancement instead of being guessed complete.
# 函数用途: 核对模型处理声明覆盖精确输入快照。
def _validate_processed_refs(
    extraction: CuratorExtraction,
    messages: dict[str, CuratorMessageInput],
    audits: dict[str, CuratorAuditInput],
) -> None:
    _validate_unresolved_ref_shape(extraction.unresolved_refs)
    processed_messages = _ref_ids(extraction.processed_message_refs, "message_id")
    processed_audits = _ref_ids(extraction.processed_audit_refs, "event_id")
    unresolved_messages = _ref_ids(extraction.unresolved_refs, "message_id")
    unresolved_audits = _ref_ids(extraction.unresolved_refs, "event_id")
    if processed_messages & unresolved_messages or processed_audits & unresolved_audits:
        raise CuratorEvidenceError("processed_and_unresolved_overlap")
    if processed_messages | unresolved_messages != set(messages):
        raise CuratorEvidenceError("message_coverage_mismatch")
    if processed_audits | unresolved_audits != set(audits):
        raise CuratorEvidenceError("audit_coverage_mismatch")


# LLM: The strict schema keeps provider compatibility by declaring both nullable stream IDs;
# host validation enforces the semantic XOR so one unresolved row cannot cover two inputs.
# 函数用途: 确保每条 unresolved 引用恰好指向一条 message 或一条 audit 事件。
def _validate_unresolved_ref_shape(refs: object) -> None:
    for ref in normalize_reference_list(refs):
        has_message = bool(str(ref.get("message_id") or "").strip())
        has_audit = bool(str(ref.get("event_id") or "").strip())
        if has_message == has_audit:
            raise CuratorEvidenceError("unresolved_ref_not_xor")


# LLM: The provider selects message_id only; the host supplies one exact continuous quote and
# full-content hash from ConversationStore, eliminating provider-authored evidence text.
# 函数用途: 将模型 message refs 替换成带宿主 quote/hash 的 canonical owner refs。
def _canonical_message_refs(
    refs: object,
    messages: dict[str, CuratorMessageInput],
) -> list[dict[str, object]]:
    canonical: list[dict[str, object]] = []
    for ref in normalize_reference_list(refs):
        message_id = str(ref.get("message_id") or "")
        source = messages.get(message_id)
        if source is None or str(ref.get("thread_id") or source.thread_id) != source.thread_id:
            raise CuratorEvidenceError("message_outside_owner_batch")
        quote = source.full_content[:300]
        canonical.append({**source.ref(), "quote": quote})
    return canonical


# LLM: Tool evidence status comes only from owner audit; a model-supplied success string cannot
# override failed, timeout, cancelled, or unknown-effect records.
# 函数用途: 将 tool refs 绑定到当前 owner audit 事件。
def _canonical_tool_refs(
    refs: object,
    by_event: dict[str, CuratorAuditInput],
    by_call: dict[str, CuratorAuditInput],
    by_operation: dict[str, CuratorAuditInput],
    *,
    require_success: bool,
    owner_id: str,
) -> list[dict[str, object]]:
    canonical: list[dict[str, object]] = []
    for ref in normalize_reference_list(refs):
        source = _tool_source(ref, by_event, by_call, by_operation)
        if source is None:
            raise CuratorEvidenceError("tool_outside_owner_batch")
        if require_success and source.status.strip().lower() not in _SUCCESS_TOOL_STATUSES:
            raise CuratorEvidenceError("tool_not_successful_terminal")
        resolved = source.ref()
        if owner_id:
            resolved["owner_id"] = owner_id
        canonical.append(resolved)
    return canonical


# LLM: Identity resolution accepts exact event/call/operation keys only and never searches
# preview text or timestamps.
# 函数用途: 根据结构化工具身份查找当前 audit 输入。
def _tool_source(
    ref: dict[str, object],
    by_event: dict[str, CuratorAuditInput],
    by_call: dict[str, CuratorAuditInput],
    by_operation: dict[str, CuratorAuditInput],
) -> CuratorAuditInput | None:
    return (
        by_event.get(str(ref.get("event_id") or ""))
        or by_call.get(str(ref.get("tool_call_id") or ref.get("call_id") or ""))
        or by_operation.get(str(ref.get("operation_id") or ""))
    )


# LLM: Cursor advancement follows only continuous processed prefixes per stream; unresolved
# input and everything after it remain eligible for safe replay.
# 函数用途: 根据验证后的处理声明计算 host-owned 下一游标。
def next_cursors(
    current_message_cursors: dict[str, str],
    current_audit_cursor: str,
    batch: CuratorInputBatch,
    extraction: CuratorExtraction,
) -> CuratorCursorAdvance:
    processed_message_ids = _ref_ids(extraction.processed_message_refs, "message_id")
    processed_audit_ids = _ref_ids(extraction.processed_audit_refs, "event_id")
    cursors, advanced_messages = _advance_message_cursors(
        current_message_cursors,
        batch.messages,
        processed_message_ids,
    )
    audit_cursor, advanced_audit = _advance_audit_cursor(
        current_audit_cursor,
        batch.audit_events,
        processed_audit_ids,
    )
    return CuratorCursorAdvance(cursors, audit_cursor, advanced_messages, advanced_audit)


# LLM: Per-thread order is inherited from ConversationStore typed reads rather than timestamp
# comparison.
# 函数用途: 推进每个线程的连续消息前缀。
def _advance_message_cursors(
    current: dict[str, str],
    messages: tuple[CuratorMessageInput, ...],
    processed: set[str],
) -> tuple[dict[str, str], int]:
    cursors = dict(current)
    grouped: dict[str, list[CuratorMessageInput]] = {}
    for message in messages:
        grouped.setdefault(message.thread_id, []).append(message)
    advanced = 0
    for thread_id, entries in grouped.items():
        for message in entries:
            if message.message_id not in processed:
                break
            cursors[thread_id] = message.message_id
            advanced += 1
    return cursors, advanced


# LLM: Audit order is the collector's canonical daily-file/line append order, never a timestamp
# sort guessed by the model.
# 函数用途: 推进 audit 的连续 processed 前缀。
def _advance_audit_cursor(
    current: str,
    events: tuple[CuratorAuditInput, ...],
    processed: set[str],
) -> tuple[str, int]:
    cursor = current
    advanced = 0
    for event in events:
        if event.event_id not in processed:
            break
        cursor = event.event_id
        advanced += 1
    return cursor, advanced


# LLM: Reference coverage uses exact non-empty IDs only; no coercion from content or list
# position is permitted.
# 函数用途: 提取 processed/unresolved 引用身份集合。
def _ref_ids(refs: object, key: str) -> set[str]:
    return {
        str(ref.get(key) or "")
        for ref in normalize_reference_list(refs)
        if str(ref.get(key) or "")
    }


__all__ = [
    "CuratorCursorAdvance",
    "CuratorEvidenceError",
    "next_cursors",
    "validate_extraction",
]
