# LLM: 插话持久回执及纯校验规则；保留原版本迁移，不依赖聚合 Store，修改须联测崩溃恢复与指纹校验。
# 模块用途: 定义插话回执、解析队列并构造迁移后的记录；调用方负责提交和锁。
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from ..runtime_errors import DataCorruptionError, runtime_error_report
from .models import GuidanceEntry, new_id, normalize_guidance_target_type
from .store_io import now


# LLM: This receipt is the durable idempotency and delivery authority for one guidance ingress.
# The JSONL row is only its FIFO projection; pending/reserved/submitted/consumed/rejected lives here.
# 类用途: 记录补充消息从等待、当前尝试预留、提交模型、确认消费到拒绝的唯一持久状态。
@dataclass(frozen=True)
class GuidanceOnceReceipt:
    dedupe_key: str
    input_digest: str
    status: str
    entry: GuidanceEntry
    updated_at: float
    attempt_id: str = ""
    submission_id: str = ""
    submitted_at: float = 0.0
    migration: dict[str, Any] = field(default_factory=dict)

    # LLM: Receipt serialization stays schema-neutral at the store boundary; callers consume
    # typed fields and must not infer delivery state from filenames or prose.
    # 函数用途: 把幂等回执转换成原子 JSON 文件可保存的字典。
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "conversation_guidance_once.v4",
            "dedupe_key": self.dedupe_key,
            "input_digest": self.input_digest,
            "status": self.status,
            "entry": self.entry.to_dict(),
            "updated_at": self.updated_at,
            "attempt_id": self.attempt_id,
            "submission_id": self.submission_id,
            "submitted_at": self.submitted_at,
            **({"migration": dict(self.migration)} if self.migration else {}),
        }

    # LLM: Invalid or partial receipt payloads fail closed so retry never invents acceptance.
    # 函数用途: 从持久化字典恢复幂等回执，并校验关键身份和状态字段。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuidanceOnceReceipt:
        entry = data.get("entry")
        schema_version = str(data.get("schema_version") or "").strip()
        status = str(data.get("status") or "").strip().lower()
        migration = data.get("migration")
        migration = dict(migration) if isinstance(migration, dict) else {}
        if schema_version == "conversation_guidance_once.v1":
            legacy_status = status
            status = {
                "pending": "pending",
                "claimed": "submitted",
                # v1 accepted meant only that Gateway had appended the row. It never proved
                # provider consumption, so the explicit migration keeps it unresolved.
                "accepted": "submitted",
                "consumed": "consumed",
                "rejected": "rejected",
            }.get(status, "")
            migration = {
                "from_schema": "conversation_guidance_once.v1",
                "from_status": legacy_status,
                "decision": "legacy_gateway_acceptance_is_not_provider_consumption",
                "migrated_at": time.time(),
            }
        elif schema_version == "conversation_guidance_once.v2":
            legacy_status = status
            status = {
                "pending": "pending",
                # v2 could not prove whether provider submission had begun, so
                # migration chooses the duplicate-safe unresolved side.
                "claimed": "submitted",
                "consumed": "consumed",
                "rejected": "rejected",
            }.get(status, "")
            migration = {
                "from_schema": "conversation_guidance_once.v2",
                "from_status": legacy_status,
                "decision": "legacy_claim_is_provider_submission_unknown",
                "migrated_at": time.time(),
            }
        elif schema_version == "conversation_guidance_once.v3":
            legacy_status = status
            migration = {
                "from_schema": "conversation_guidance_once.v3",
                "from_status": legacy_status,
                "decision": "legacy_submission_has_no_atomic_batch_identity",
                "migrated_at": time.time(),
            }
        elif schema_version != "conversation_guidance_once.v4":
            status = ""
        if not isinstance(entry, dict) or status not in {
            "pending",
            "reserved",
            "submitted",
            "consumed",
            "rejected",
        }:
            raise DataCorruptionError("conversation guidance receipt is invalid")
        receipt = cls(
            dedupe_key=str(data.get("dedupe_key") or "").strip(),
            input_digest=str(data.get("input_digest") or "").strip(),
            status=status,
            entry=GuidanceEntry.from_dict(entry),
            updated_at=float(data.get("updated_at") or 0.0),
            attempt_id=str(data.get("attempt_id") or "").strip(),
            submission_id=(
                str(data.get("submission_id") or "").strip()
                or ("legacy-unknown" if status == "submitted" else "")
            ),
            submitted_at=float(data.get("submitted_at") or 0.0),
            migration=migration,
        )
        if not receipt.dedupe_key or not receipt.input_digest or not receipt.entry.guidance_id:
            raise DataCorruptionError("conversation guidance receipt identity is invalid")
        _validate_guidance_once_receipt(receipt, legacy=bool(migration))
        return receipt


# LLM: 只投影送达时间，不修改原队列记录或回执状态；与 pending 读取分开维护。
# 函数用途: 为队列条目附加送达索引中的时间。
def _with_guidance_delivered_at(entry: GuidanceEntry, delivered: dict[str, float]) -> GuidanceEntry:
    return replace(entry, delivered_at=float(delivered.get(entry.guidance_id) or 0.0))

# LLM: 逐条保留解析错误与行号，不以损坏行阻塞其它有效消息；调用方决定错误展示。
# 函数用途: 把队列对象解析成消息和错误列表。
def _guidance_entries(
    rows: list[dict[str, Any]],
    delivered: dict[str, float],
) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
    entries: list[GuidanceEntry] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            entries.append(_with_guidance_delivered_at(GuidanceEntry.from_dict(row), delivered))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.guidance.parse")
            report["row_index"] = index
            errors.append(report)
    return entries, errors

# LLM: Guidance construction is shared by ordinary append and idempotent ingress. Retry-only
# fields such as ``now`` never alter an already prepared entry.
# 函数用途: 校验补充消息并构造一条尚未写盘的 guidance 记录。
def _guidance_entry_from_request(
    request: dict[str, Any],
    *,
    guidance_id: str = "",
) -> GuidanceEntry:
    target_type = normalize_guidance_target_type(request.get("target_type"))
    target_id = str(request.get("target_id") or "").strip()
    message = str(request.get("message") or "").strip()
    if not target_type or not target_id:
        raise ValueError("target_type and target_id are required")
    if not message:
        raise ValueError("guidance message is required")
    metadata = request.get("metadata")
    return GuidanceEntry(
        guidance_id=str(guidance_id or "").strip() or new_id("guidance"),
        target_type=target_type,
        target_id=target_id,
        message=message,
        sender=str(request.get("sender") or "").strip(),
        priority=str(request.get("priority") or "normal").strip() or "normal",
        delivery=str(request.get("delivery") or "next_turn").strip() or "next_turn",
        created_at=now(request.get("now")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )

# LLM: The digest covers every semantic input that could make reuse unsafe, but excludes time and
# the generated guidance id so a transport retry remains byte-independent.
# 函数用途: 为幂等键计算正文、目标和结构化元数据的稳定指纹。
def _guidance_input_digest(request: dict[str, Any]) -> str:
    metadata = request.get("metadata")
    canonical_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    canonical_metadata.pop("dedupe_key", None)
    canonical = {
        "target_type": normalize_guidance_target_type(request.get("target_type")),
        "target_id": str(request.get("target_id") or "").strip(),
        "message": str(request.get("message") or "").strip(),
        "sender": str(request.get("sender") or "").strip(),
        "priority": str(request.get("priority") or "normal").strip() or "normal",
        "delivery": str(request.get("delivery") or "next_turn").strip() or "next_turn",
        "metadata": canonical_metadata,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

# LLM: Receipt rebind changes only host-owned attempt routing. Reconstructing the semantic request
# here keeps the input digest and duplicate-ingress validation aligned after recovery.
# 函数用途: 从持久 guidance 记录重建幂等指纹所需的稳定输入字段。
def _guidance_request_from_entry(entry: GuidanceEntry) -> dict[str, Any]:
    return {
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "message": entry.message,
        "sender": entry.sender,
        "priority": entry.priority,
        "delivery": entry.delivery,
        "metadata": dict(entry.metadata) if isinstance(entry.metadata, dict) else {},
    }

# LLM: Only pending/reserved rows proven to belong to a dead attempt reach this helper. It records
# the structured lineage and resets every provider-bound field before the new attempt may claim it.
# 函数用途: 生成一条安全改绑到恢复 attempt 的待处理插话回执。
def _rebound_guidance_receipt(
    receipt: GuidanceOnceReceipt,
    *,
    old_turn_id: str,
    recovered_turn_id: str,
) -> GuidanceOnceReceipt:
    metadata = dict(receipt.entry.metadata) if isinstance(receipt.entry.metadata, dict) else {}
    metadata["expected_turn_id"] = recovered_turn_id
    entry = replace(receipt.entry, metadata=metadata)
    migration = dict(receipt.migration)
    history = list(migration.get("turn_rebinds") or [])
    history.append(
        {
            "from_turn_id": old_turn_id,
            "to_turn_id": recovered_turn_id,
            "reason": "dead_attempt_before_provider_submission",
            "rebound_at": time.time(),
        }
    )
    migration["turn_rebinds"] = history[-20:]
    return replace(
        receipt,
        input_digest=_guidance_input_digest(_guidance_request_from_entry(entry)),
        status="pending",
        entry=entry,
        updated_at=time.time(),
        attempt_id="",
        submission_id="",
        submitted_at=0.0,
        migration=migration,
    )

# LLM: Legacy runner-prompt injection marked delivery before provider I/O. Such rows cannot be
# replayed safely; migrate them to explicit submission-unknown instead of pretending consumption.
# 函数用途: 把旧版“拼提示词即送达”的模糊状态封存为已提交但结果未知，阻止恢复时重复插话。
def _legacy_prompt_submission_unknown_receipt(
    receipt: GuidanceOnceReceipt,
    *,
    old_turn_id: str,
    delivered_at: float,
) -> GuidanceOnceReceipt:
    migration = dict(receipt.migration)
    migration["legacy_runner_prompt_delivery"] = {
        "turn_id": old_turn_id,
        "decision": "provider_submission_unknown_no_replay",
        "observed_at": time.time(),
    }
    return replace(
        receipt,
        status="submitted",
        updated_at=time.time(),
        attempt_id=receipt.attempt_id or old_turn_id,
        submission_id=f"legacy-runner-prompt-unknown:{receipt.entry.guidance_id}",
        submitted_at=max(float(delivered_at), 1e-9),
        migration=migration,
    )

# LLM: Queue JSONL is the immutable ingress record while a pending receipt may move between
# physical attempts. Permit only that one host-owned metadata difference; all user semantics and
# identities must remain byte-equivalent.
# 函数用途: 校验恢复改绑后的权威回执仍与最初队列记录表示同一条用户消息。
def _guidance_queue_entry_matches_receipt(
    queued: GuidanceEntry,
    receipt_entry: GuidanceEntry,
) -> bool:
    if queued.to_dict() == receipt_entry.to_dict():
        return True
    queued_payload = queued.to_dict()
    receipt_payload = receipt_entry.to_dict()
    queued_metadata = dict(queued_payload.get("metadata") or {})
    receipt_metadata = dict(receipt_payload.get("metadata") or {})
    queued_turn = str(queued_metadata.pop("expected_turn_id", "") or "").strip()
    receipt_turn = str(receipt_metadata.pop("expected_turn_id", "") or "").strip()
    queued_payload["metadata"] = queued_metadata
    receipt_payload["metadata"] = receipt_metadata
    return bool(queued_turn and receipt_turn) and queued_payload == receipt_payload

# LLM: Only receipt-authored rebind lineage may authorize stale projection repair. Malformed
# migration data fails closed instead of allowing an arbitrary old turn index to be overwritten.
# 函数用途: 提取一条补充消息曾经绑定过的旧 attempt，并校验迁移链最终指向当前 attempt。
def _guidance_rebound_from_turn_ids(receipt: GuidanceOnceReceipt) -> set[str]:
    history = receipt.migration.get("turn_rebinds") if receipt.migration else None
    if history is None:
        return set()
    if not isinstance(history, list):
        raise DataCorruptionError("conversation guidance rebind history is invalid")
    metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
    current_turn = str(metadata.get("expected_turn_id") or "").strip()
    old_turns: set[str] = set()
    last_target = ""
    for item in history:
        if not isinstance(item, dict):
            raise DataCorruptionError("conversation guidance rebind record is invalid")
        old_turn = str(item.get("from_turn_id") or "").strip()
        new_turn = str(item.get("to_turn_id") or "").strip()
        if not old_turn or not new_turn or old_turn == new_turn:
            raise DataCorruptionError("conversation guidance rebind turn ids are invalid")
        old_turns.add(old_turn)
        last_target = new_turn
    if history and (not current_turn or last_target != current_turn):
        raise DataCorruptionError("conversation guidance rebind target is inconsistent")
    return old_turns

# LLM: The embedded entry, not the stored digest string, reconstructs receipt identity. Server-only
# dedupe metadata is removed exactly as append does, while target/message and state combinations are
# validated before any queue repair can trust the row.
# 函数用途: 重算幂等补充回执的正文指纹，并校验状态、尝试和提交字段是否自洽。
def _validate_guidance_once_receipt(
    receipt: GuidanceOnceReceipt,
    *,
    legacy: bool,
) -> None:
    entry = receipt.entry
    metadata = dict(entry.metadata) if isinstance(entry.metadata, dict) else {}
    if str(metadata.get("dedupe_key") or "").strip() != receipt.dedupe_key:
        raise DataCorruptionError("conversation guidance receipt metadata key mismatch")
    reconstructed = _guidance_request_from_entry(entry)
    if (
        normalize_guidance_target_type(entry.target_type) != entry.target_type
        or not str(entry.target_id or "").strip()
        or not str(entry.message or "").strip()
        or _guidance_input_digest(reconstructed) != receipt.input_digest
    ):
        raise DataCorruptionError("conversation guidance receipt input digest mismatch")
    if (
        not math.isfinite(receipt.updated_at)
        or not math.isfinite(receipt.submitted_at)
        or receipt.updated_at < 0
        or receipt.submitted_at < 0
    ):
        raise DataCorruptionError("conversation guidance receipt timestamp is invalid")
    if legacy:
        return
    state_fields_valid = {
        "pending": (
            not receipt.attempt_id
            and not receipt.submission_id
            and receipt.submitted_at == 0
        ),
        "reserved": (
            bool(receipt.attempt_id)
            and not receipt.submission_id
            and receipt.submitted_at == 0
        ),
        "submitted": (
            bool(receipt.attempt_id)
            and bool(receipt.submission_id)
            and receipt.submitted_at > 0
        ),
        "consumed": (
            bool(receipt.attempt_id)
            and bool(receipt.submission_id)
            and receipt.submitted_at > 0
        ),
        "rejected": not receipt.submission_id and receipt.submitted_at == 0,
    }
    if not state_fields_valid.get(receipt.status, False):
        raise DataCorruptionError("conversation guidance receipt state fields are invalid")
