# LLM: 原生协作交接板（Handoff Board）实现：
#   基于已有的 CollaborationStore（cases/requests/evidence/decisions + .history-index.sqlite3）与 ConversationStore（observations/wakes）；
#   不新建任何独立的队列文件或外部数据库。
#   严格执行：
#   1. Submission != receipt != acceptance 单向状态机
#   2. 稳定 ID 与 payload 摘要去重（一致则幂等返回，冲突则 IDEMPOTENCY_CONFLICT 失败关闭）
#   3. 同 case 下活跃交接项的 exclusive_write_set 互斥判定（重叠则 HANDOFF_WRITESET_OVERLAP 失败关闭）
#   4. 归档按 YYYY-MM-DD/ 日期分桶与有界游标分页。
# 模块用途: 统一管理多代理（内部 run 与外部交互式助手）的交接项生命周期、送达确认、验收判定与归档。
"""Native collaboration handoff board service and state machine."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..common.nofollow_fs import ensure_private_dir
from ..gateway_parts.io import write_json_file_atomic
from .models import (
    CollaborationCase,
    EvidencePacket,
    HandoffBoardItem,
    new_decision_id,
    new_handoff_item_id,
)

if TYPE_CHECKING:
    from ..conversation import ConversationStore
    from .store import CollaborationStore


# LLM: 交接项通用运行时异常，携带机器可读 error_code 与 details。
# 类用途: 协作交接板基础错误类型。
class HandoffError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "HANDOFF_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


# LLM: 稳定 ID 下载荷摘要冲突引发的失败关闭异常。
# 类用途: 标识交接板操作中相同 ID 载荷不一致冲突。
class IdempotencyConflictError(HandoffError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="IDEMPOTENCY_CONFLICT", details=details)


# LLM: 同 case 内活跃交接项独占写入集发生重叠时触发的失败关闭异常。
# 类用途: 标识交接项排他路径撞车错误。
class HandoffWriteSetOverlapError(HandoffError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="HANDOFF_WRITESET_OVERLAP", details=details)


# LLM: 接收方 ACK 时上报的冻结输入版本与交接项要求不匹配时触发的失败关闭异常。
# 类用途: 标识冻结输入版本不一致错误。
class FrozenInputVersionMismatchError(HandoffError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="FROZEN_INPUT_VERSION_MISMATCH", details=details)


# LLM: 违反 Submission != receipt != acceptance 流程的状态流转异常。
# 类用途: 标识交接板非法状态迁移错误。
class InvalidStateTransitionError(HandoffError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="INVALID_STATE_TRANSITION", details=details)


# LLM: 归一化路径列表，去除首尾空白与冗余分段，排序去重。
# 函数用途: 规范化文件写入集合路径元组。
def normalize_paths(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in paths:
        text = str(item or "").strip()
        if not text:
            continue
        cleaned = os.path.normpath(text)
        if cleaned not in normalized:
            normalized.append(cleaned)
    normalized.sort()
    return tuple(normalized)


# LLM: 检测两个路径集合之间是否存在精确重合或父子目录重叠。
# 函数用途: 判断两组写入路径是否存在冲突。
def paths_overlap(paths_a: tuple[str, ...], paths_b: tuple[str, ...]) -> list[tuple[str, str]]:
    overlaps: list[tuple[str, str]] = []
    for a in paths_a:
        parts_a = Path(a).parts
        for b in paths_b:
            parts_b = Path(b).parts
            if a == b:
                overlaps.append((a, b))
                continue
            if len(parts_a) < len(parts_b) and parts_b[:len(parts_a)] == parts_a or len(parts_b) < len(parts_a) and parts_a[:len(parts_b)] == parts_b:
                overlaps.append((a, b))
    return overlaps


# LLM: 计算任意字典载荷的确定性 SHA-256 摘要，用于操作幂等与防篡改核验。
# 函数用途: 生成字典数据的稳定散列字符串。
def compute_payload_digest(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# LLM: 活跃写状态集合，处于这些状态的交接项占用独占写入集。
# 函数用途: 判断交接项当前是否处于独占写入活跃状态。
def is_active_handoff_state(state: str) -> bool:
    return state in {"published", "delivered", "acked", "submitted", "changes_requested"}


# LLM: 原生交接板服务，基于 CollaborationStore 提供原子发布、交付、ACK、提交与验收判定。
# 类用途: 管理跨代理交付物的完整状态机与持久化。
class HandoffBoardService:
    # LLM: 初始化交接板服务，复用既有存储实例，可选挂载对话唤醒队列与工作区根路径。
    # 函数用途: 构造交接板服务对象。
    def __init__(
        self,
        store: CollaborationStore,
        conversation_store: ConversationStore | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.store = store
        self.conversation_store = conversation_store
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.archive_dir = self.store.root / "archived_handoff_items"

    def _require_existing_case(self, case_id: str) -> CollaborationCase:
        if not case_id or not isinstance(case_id, str):
            raise ValueError("invalid collaboration case_id: empty or non-string")
        if "/" in case_id or "\\" in case_id or ".." in case_id:
            raise ValueError(f"invalid collaboration case_id: {case_id}")
        return self.store.load_case(case_id)

    # LLM: 发布交接项到指定 case；检查重叠写入集并计算载荷摘要；支持幂等去重。
    # 函数用途: 在交接板上发布新交接项，状态进入 published。
    def publish_item(
        self,
        *,
        case_id: str,
        request_id: str,
        responsible_actor: str,
        actor_kind: Literal["internal_run", "external_assistant"] | str = "internal_run",
        frozen_input_version: str,
        exclusive_write_set: tuple[str, ...] | list[str] = (),
        idempotency_key: str = "",
        item_id: str = "",
        metadata: dict[str, Any] | None = None,
        allow_serialized_overlap: bool = False,
        now: float | None = None,
        recipient_validator: Callable[[], None] | None = None,
    ) -> HandoffBoardItem:
        self._require_existing_case(case_id)
        with self.store.handoff_case_lock(case_id):
            self.store.load_case(case_id)
            current_time_val = float(now if now is not None else time.time())
            norm_write_set = normalize_paths(exclusive_write_set)
            actual_item_id = item_id.strip() if item_id and item_id.strip() else new_handoff_item_id()
            stable_key = idempotency_key.strip() or actual_item_id

            digest_payload = {
                "case_id": case_id,
                "request_id": request_id,
                "responsible_actor": responsible_actor,
                "actor_kind": actor_kind,
                "frozen_input_version": frozen_input_version,
                "exclusive_write_set": list(norm_write_set),
            }
            current_digest = compute_payload_digest(digest_payload)

            # 检查是否已存在同 item_id 或同 stable_key 的交接项
            existing_items = self.store.case_handoff_items(case_id)
            for existing in existing_items:
                existing_stable_key = existing.idempotency_key or existing.item_id
                if existing.item_id == actual_item_id or existing_stable_key == stable_key:
                    existing_digest = str(existing.metadata.get("payload_digest") or "")
                    if not existing_digest:
                        existing_digest = compute_payload_digest({
                            "case_id": existing.case_id,
                            "request_id": existing.request_id,
                            "responsible_actor": existing.responsible_actor,
                            "actor_kind": existing.actor_kind,
                            "frozen_input_version": existing.frozen_input_version,
                            "exclusive_write_set": list(existing.exclusive_write_set),
                        })
                    if existing_digest == current_digest:
                        return existing
                    raise IdempotencyConflictError(
                        f"IDEMPOTENCY_CONFLICT: Item {actual_item_id} (key={stable_key}) already exists with conflicting payload",
                        details={
                            "item_id": actual_item_id,
                            "idempotency_key": stable_key,
                            "existing_digest": existing_digest,
                            "attempted_digest": current_digest,
                        },
                    )

            # 原回执去重在先；仅新发布核宿主收件身份，终态不应使已成功回执失效。
            if recipient_validator is not None:
                recipient_validator()

            # 独占写入集重叠检查（同 case 内其他处于活跃状态的交接项）
            if norm_write_set and not allow_serialized_overlap:
                for active_item in existing_items:
                    if is_active_handoff_state(active_item.state) and active_item.exclusive_write_set:
                        overlaps = paths_overlap(norm_write_set, active_item.exclusive_write_set)
                        if overlaps:
                            raise HandoffWriteSetOverlapError(
                                f"HANDOFF_WRITESET_OVERLAP: Exclusive write set overlaps with active item {active_item.item_id}: {overlaps}",
                                details={
                                    "attempted_item_id": actual_item_id,
                                    "active_item_id": active_item.item_id,
                                    "active_state": active_item.state,
                                    "overlapping_paths": overlaps,
                                },
                            )

            meta = copy.deepcopy(metadata or {})
            meta["payload_digest"] = current_digest

            delivery_receipt: dict[str, object] = {}
            # 内部 run 代理且挂接 conversation_store 时，记录观察事件与唤醒标识
            if actor_kind == "internal_run" and self.conversation_store is not None:
                case = self.store.load_case(case_id)
                if case and case.thread_id:
                    obs = self.conversation_store.observations.append({
                        "thread_id": case.thread_id,
                        "event_type": "handoff_item_published",
                        "summary": f"交接项已就绪: {actual_item_id} (负责: {responsible_actor})",
                        "urgency": case.priority,
                        "source_agent_id": "handoff_board",
                        "root_task_id": case.task_id,
                        "evidence_refs": [],
                        "requires_main_agent": False,
                        "requires_llm_report": False,
                        "now": current_time_val,
                        "metadata": {"item_id": actual_item_id, "case_id": case_id},
                    })
                    delivery_receipt["observation_id"] = getattr(obs, "observation_id", "")

            new_item = HandoffBoardItem(
                item_id=actual_item_id,
                case_id=case_id,
                request_id=request_id,
                responsible_actor=responsible_actor,
                actor_kind=actor_kind,
                frozen_input_version=frozen_input_version,
                exclusive_write_set=norm_write_set,
                state="published",
                delivery_receipt=delivery_receipt,
                recipient_ack=None,
                acceptance_verdict=None,
                evidence_ids=(),
                idempotency_key=stable_key,
                created_at=current_time_val,
                updated_at=current_time_val,
                metadata=meta,
            )
            self.store.append_handoff_item(new_item)
            return new_item

    # LLM: 记录传输层投递凭据，将交接项由 published 推进到 delivered。
    # 函数用途: 登记交付传输结果，支持幂等去重。
    def record_delivery(
        self,
        *,
        case_id: str,
        item_id: str,
        delivery_receipt: dict[str, object],
        delivery_id: str = "",
        now: float | None = None,
    ) -> HandoffBoardItem:
        self._require_existing_case(case_id)
        with self.store.handoff_case_lock(case_id):
            self.store.load_case(case_id)
            current_time_val = float(now if now is not None else time.time())
            item = self.store.load_handoff_item(case_id, item_id)
            if item is None:
                raise HandoffError(f"Handoff item not found: {item_id} in case {case_id}")

            delivery_key = delivery_id.strip() or f"deliv_{item_id}"
            receipt_digest = compute_payload_digest(delivery_receipt)

            if item.state in {"delivered", "acked", "submitted", "accepted", "changes_requested", "closed"}:
                prior_digest = str(item.metadata.get("delivery_digest") or "")
                if prior_digest == receipt_digest:
                    return item
                if item.state == "delivered":
                    raise IdempotencyConflictError(
                        f"IDEMPOTENCY_CONFLICT: Item {item_id} already has a delivery receipt with different payload",
                        details={"item_id": item_id, "delivery_id": delivery_key},
                    )
                return item

            if item.state != "published":
                raise InvalidStateTransitionError(
                    f"Cannot record delivery for item {item_id} in state {item.state}",
                    details={"item_id": item_id, "current_state": item.state},
                )

            merged_receipt = dict(item.delivery_receipt)
            merged_receipt.update(delivery_receipt)
            merged_receipt["delivery_id"] = delivery_key

            meta = copy.deepcopy(item.metadata)
            meta["delivery_digest"] = receipt_digest

            updated = HandoffBoardItem(
                item_id=item.item_id,
                case_id=item.case_id,
                request_id=item.request_id,
                responsible_actor=item.responsible_actor,
                actor_kind=item.actor_kind,
                frozen_input_version=item.frozen_input_version,
                exclusive_write_set=item.exclusive_write_set,
                state="delivered",
                delivery_receipt=merged_receipt,
                recipient_ack=item.recipient_ack,
                acceptance_verdict=item.acceptance_verdict,
                evidence_ids=item.evidence_ids,
                idempotency_key=item.idempotency_key,
                created_at=item.created_at,
                updated_at=current_time_val,
                metadata=meta,
            )
            self.store.append_handoff_item(updated)
            return updated

    # LLM: 接收方显式确认接收，强制校验 frozen_input_version 严格匹配；推进到 acked。
    # 函数用途: 登记接收方确认回执，核验冻结输入版本。
    def record_ack(
        self,
        *,
        case_id: str,
        item_id: str,
        recipient_ack: dict[str, object],
        frozen_input_version: str | None = None,
        ack_id: str = "",
        now: float | None = None,
    ) -> HandoffBoardItem:
        self._require_existing_case(case_id)
        with self.store.handoff_case_lock(case_id):
            self.store.load_case(case_id)
            current_time_val = float(now if now is not None else time.time())
            item = self.store.load_handoff_item(case_id, item_id)
            if item is None:
                raise HandoffError(f"Handoff item not found: {item_id} in case {case_id}")

            ack_key = ack_id.strip() or f"ack_{item_id}"
            ack_digest = compute_payload_digest(recipient_ack)

            # 稳定 ID 幂等性检测
            if item.state in {"acked", "submitted", "accepted", "changes_requested", "closed"}:
                prior_ack_digest = str(item.metadata.get("ack_digest") or "")
                if prior_ack_digest == ack_digest:
                    return item
                if item.state == "acked":
                    raise IdempotencyConflictError(
                        f"IDEMPOTENCY_CONFLICT: Item {item_id} already has an ACK with different payload",
                        details={"item_id": item_id, "ack_id": ack_key},
                    )

            # 输入版本严格校验
            reported_version = frozen_input_version or str(recipient_ack.get("frozen_input_version") or "")
            if reported_version != item.frozen_input_version:
                raise FrozenInputVersionMismatchError(
                    f"FROZEN_INPUT_VERSION_MISMATCH: Item {item_id} requires version {item.frozen_input_version!r}, got {reported_version!r}",
                    details={
                        "item_id": item_id,
                        "expected_version": item.frozen_input_version,
                        "reported_version": reported_version,
                    },
                )

            # 状态机约束：必须是 published, delivered, 或 changes_requested
            if item.state not in {"published", "delivered", "changes_requested"}:
                raise InvalidStateTransitionError(
                    f"Cannot record ACK for item {item_id} in state {item.state}",
                    details={"item_id": item_id, "current_state": item.state},
                )

            meta = copy.deepcopy(item.metadata)
            meta["ack_digest"] = ack_digest

            acked_item = HandoffBoardItem(
                item_id=item.item_id,
                case_id=item.case_id,
                request_id=item.request_id,
                responsible_actor=item.responsible_actor,
                actor_kind=item.actor_kind,
                frozen_input_version=item.frozen_input_version,
                exclusive_write_set=item.exclusive_write_set,
                state="acked",
                delivery_receipt=item.delivery_receipt,
                recipient_ack=dict(recipient_ack),
                acceptance_verdict=item.acceptance_verdict,
                evidence_ids=item.evidence_ids,
                idempotency_key=item.idempotency_key,
                created_at=item.created_at,
                updated_at=current_time_val,
                metadata=meta,
            )
            self.store.append_handoff_item(acked_item)
            return acked_item

    # LLM: 负责代理提交交付证据包；推进至 submitted（绝对禁止自动转入 accepted）。
    # 函数用途: 登记交付物证据包，推进状态为 submitted 供人工或终审判定。
    def submit_handoff(
        self,
        *,
        case_id: str,
        item_id: str,
        evidence_ids: tuple[str, ...] | list[str] = (),
        evidence_packets: list[EvidencePacket] | None = None,
        submission_payload: dict[str, object] | None = None,
        submission_id: str = "",
        now: float | None = None,
    ) -> HandoffBoardItem:
        self._require_existing_case(case_id)
        with self.store.handoff_case_lock(case_id):
            self.store.load_case(case_id)
            current_time_val = float(now if now is not None else time.time())
            item = self.store.load_handoff_item(case_id, item_id)
            if item is None:
                raise HandoffError(f"Handoff item not found: {item_id} in case {case_id}")

            all_evidence_ids = list(item.evidence_ids)
            for eid in evidence_ids:
                s_eid = str(eid).strip()
                if s_eid and s_eid not in all_evidence_ids:
                    all_evidence_ids.append(s_eid)

            if evidence_packets:
                for packet in evidence_packets:
                    if isinstance(packet, dict):
                        ev = self.store.submit_evidence(packet)
                        if ev.evidence_id and ev.evidence_id not in all_evidence_ids:
                            all_evidence_ids.append(ev.evidence_id)
                    elif hasattr(packet, "to_dict"):
                        ev = self.store.submit_evidence(packet.to_dict())
                        if ev.evidence_id and ev.evidence_id not in all_evidence_ids:
                            all_evidence_ids.append(ev.evidence_id)
                    elif hasattr(packet, "evidence_id"):
                        eid = str(packet.evidence_id or "").strip()
                        if eid and eid not in all_evidence_ids:
                            all_evidence_ids.append(eid)

            sub_digest = compute_payload_digest({
                "evidence_ids": sorted(all_evidence_ids),
                "payload": submission_payload or {},
            })

            if item.state in {"submitted", "accepted", "changes_requested", "closed"}:
                prior_sub_digest = str(item.metadata.get("submission_digest") or "")
                if prior_sub_digest == sub_digest:
                    return item
                if item.state == "submitted":
                    raise IdempotencyConflictError(
                        f"IDEMPOTENCY_CONFLICT: Item {item_id} already submitted with different evidence payload",
                        details={"item_id": item_id, "submission_id": submission_id},
                    )

            if item.state not in {"acked", "changes_requested"}:
                raise InvalidStateTransitionError(
                    f"Cannot submit handoff for item {item_id} in state {item.state}; must be acked first",
                    details={"item_id": item_id, "current_state": item.state},
                )

            meta = copy.deepcopy(item.metadata)
            meta["submission_digest"] = sub_digest
            if submission_payload:
                meta["submission_payload"] = submission_payload

            submitted_item = HandoffBoardItem(
                item_id=item.item_id,
                case_id=item.case_id,
                request_id=item.request_id,
                responsible_actor=item.responsible_actor,
                actor_kind=item.actor_kind,
                frozen_input_version=item.frozen_input_version,
                exclusive_write_set=item.exclusive_write_set,
                state="submitted",  # 铁律：永远不能自动 accepted！
                delivery_receipt=item.delivery_receipt,
                recipient_ack=item.recipient_ack,
                acceptance_verdict=item.acceptance_verdict,
                evidence_ids=tuple(all_evidence_ids),
                idempotency_key=item.idempotency_key,
                created_at=item.created_at,
                updated_at=current_time_val,
                metadata=meta,
            )
            self.store.append_handoff_item(submitted_item)
            return submitted_item

    # LLM: 协调器或授权父级执行显式验收判定（accepted 或 changes_requested）。
    # 函数用途: 对已提交的交接项执行人工或终审裁决。
    def decide_acceptance(
        self,
        *,
        case_id: str,
        item_id: str,
        verdict: Literal["accepted", "changes_requested"] | str,
        reviewer_principal: str,
        reason: str = "",
        metadata: dict[str, object] | None = None,
        now: float | None = None,
        frozen_input_version: str = "",
        submission_digest: str = "",
    ) -> HandoffBoardItem:
        self._require_existing_case(case_id)
        with self.store.handoff_case_lock(case_id):
            self.store.load_case(case_id)
            current_time_val = float(now if now is not None else time.time())
            item = self.store.load_handoff_item(case_id, item_id)
            if item is None:
                raise HandoffError(f"Handoff item not found: {item_id} in case {case_id}")

            if reviewer_principal.strip() == item.responsible_actor:
                raise HandoffError(
                    "SELF_REVIEW_FORBIDDEN: responsible_actor cannot accept own handoff item",
                    error_code="SELF_REVIEW_FORBIDDEN",
                )

            if verdict not in {"accepted", "changes_requested"}:
                raise HandoffError(f"Invalid acceptance verdict: {verdict!r}")

            # 旧调用只能读回同一主体已签的原票，不能因省略绑定字段生成新票。
            prior = item.acceptance_verdict or {}
            same_vote = (item.state == verdict and prior.get("verdict") == verdict
                         and prior.get("reviewer_principal") == reviewer_principal)
            bound_recheck = (bool(frozen_input_version and submission_digest)
                             and frozen_input_version == prior.get("frozen_input_version") == item.frozen_input_version
                             and submission_digest == prior.get("submission_digest") == item.metadata.get("submission_digest"))
            if same_vote and ((not frozen_input_version and not submission_digest) or bound_recheck):
                return item
            if item.state != "submitted":
                raise InvalidStateTransitionError(
                    f"Cannot decide acceptance on item {item_id} in state {item.state}; must be submitted first",
                    details={"item_id": item_id, "current_state": item.state},
                )

            if not frozen_input_version or not submission_digest:
                raise HandoffError(
                    "新验收票必须带已读的 frozen_input_version 和 metadata.submission_digest；"
                    "本次未签票。先用 list_items 读取指定 case_id/item_id，再调用 "
                    "decide_acceptance(case_id=原值,item_id=原值,frozen_input_version=原版本,"
                    "submission_digest=原摘要,verdict=accepted)。不要猜值或原样重试。",
                    error_code="TOOL_INVALID_ARGUMENTS",
                    details={"reason": "HANDOFF_REVIEW_BINDING_REQUIRED", "effect_outcome": "not_started"},
                )
            if frozen_input_version != item.frozen_input_version:
                raise FrozenInputVersionMismatchError(
                    "验收所读 frozen_input_version 已不匹配；未签票，请重新读取原交接项。",
                    details={"item_id": item_id, "field": "frozen_input_version"},
                )
            if submission_digest != str(item.metadata.get("submission_digest") or ""):
                raise IdempotencyConflictError(
                    "验收所读 submission_digest 已过期；未签票，请读取并重新核对当前提交后再决定。",
                    details={"item_id": item_id, "field": "submission_digest"},
                )

            verdict_record = {
                "verdict": verdict,
                "reviewer_principal": reviewer_principal,
                "reason": reason,
                "decided_at": current_time_val,
                "metadata": metadata or {},
                "frozen_input_version": item.frozen_input_version,
                "submission_digest": submission_digest,
            }

            meta = copy.deepcopy(item.metadata)
            meta["verdict_history"] = meta.get("verdict_history", [])
            if isinstance(meta["verdict_history"], list):
                meta["verdict_history"].append(verdict_record)

            updated_item = HandoffBoardItem(
                item_id=item.item_id,
                case_id=item.case_id,
                request_id=item.request_id,
                responsible_actor=item.responsible_actor,
                actor_kind=item.actor_kind,
                frozen_input_version=item.frozen_input_version,
                exclusive_write_set=item.exclusive_write_set,
                state=verdict,
                delivery_receipt=item.delivery_receipt,
                recipient_ack=item.recipient_ack,
                acceptance_verdict=verdict_record,
                evidence_ids=item.evidence_ids,
                idempotency_key=item.idempotency_key,
                created_at=item.created_at,
                updated_at=current_time_val,
                metadata=meta,
            )
            self.store.append_handoff_item(updated_item)
            return updated_item

    # LLM: 显式关闭交接项，解除对写入集合的占用。
    # 函数用途: 将交接项置为 closed 终态。
    def close_item(
        self,
        *,
        case_id: str,
        item_id: str,
        reason: str = "",
        now: float | None = None,
    ) -> HandoffBoardItem:
        self._require_existing_case(case_id)
        with self.store.handoff_case_lock(case_id):
            self.store.load_case(case_id)
            current_time_val = float(now if now is not None else time.time())
            item = self.store.load_handoff_item(case_id, item_id)
            if item is None:
                raise HandoffError(f"Handoff item not found: {item_id} in case {case_id}")

            meta = copy.deepcopy(item.metadata)
            meta["closed_reason"] = reason
            meta["closed_at"] = current_time_val

            closed_item = HandoffBoardItem(
                item_id=item.item_id,
                case_id=item.case_id,
                request_id=item.request_id,
                responsible_actor=item.responsible_actor,
                actor_kind=item.actor_kind,
                frozen_input_version=item.frozen_input_version,
                exclusive_write_set=item.exclusive_write_set,
                state="closed",
                delivery_receipt=item.delivery_receipt,
                recipient_ack=item.recipient_ack,
                acceptance_verdict=item.acceptance_verdict,
                evidence_ids=item.evidence_ids,
                idempotency_key=item.idempotency_key,
                created_at=item.created_at,
                updated_at=current_time_val,
                metadata=meta,
            )
            self.store.append_handoff_item(closed_item)
            return closed_item

    # LLM: 按条件分页列出交接项；支持限制条数与游标分页。
    # 函数用途: 检索交接项列表与下一页游标。
    def list_items(
        self,
        *,
        case_id: str | None = None,
        state: str | None = None,
        responsible_actor: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        bounded_limit = max(1, min(limit, 200))
        offset = 0
        if cursor:
            try:
                decoded = base64.b64decode(cursor.encode("ascii")).decode("utf-8")
                offset = int(decoded)
            except Exception:
                offset = 0

        raw_items: list[HandoffBoardItem] = []
        if case_id:
            self._require_existing_case(case_id)
            raw_items = self.store.case_handoff_items(case_id)
        else:
            raw_items = self.store.list_all_handoff_items(
                state=state,
                responsible_actor=responsible_actor,
            )

        filtered: list[HandoffBoardItem] = []
        for item in raw_items:
            if state is not None and item.state != state:
                continue
            if responsible_actor is not None and item.responsible_actor != responsible_actor:
                continue
            filtered.append(item)

        total_scanned = len(filtered)
        page_items = filtered[offset : offset + bounded_limit]
        next_offset = offset + len(page_items)
        next_cursor = None
        if next_offset < total_scanned:
            next_cursor = base64.b64encode(str(next_offset).encode("utf-8")).decode("ascii")

        return {
            "items": [item.to_dict() for item in page_items],
            "total_count": total_scanned,
            "next_cursor": next_cursor,
        }

    # LLM: 将已 closed 或 accepted 的陈旧交接项归档到按 YYYY-MM-DD/ 分桶的目录中。
    # 函数用途: 定期归档终态交接项，保持主活跃账本紧凑。
    def archive_closed_items(
        self,
        *,
        case_id: str | None = None,
        before_timestamp: float | None = None,
        now: float | None = None,
    ) -> list[str]:
        current_time_val = float(now if now is not None else time.time())
        archived_ids: list[str] = []

        if case_id:
            target_case_ids = [case_id]
        else:
            if not self.store.handoff_items_dir.exists():
                return archived_ids
            target_case_ids = [
                p.stem for p in sorted(self.store.handoff_items_dir.glob("*.jsonl"))
            ]

        for cid in target_case_ids:
            with self.store.handoff_case_lock(cid):
                case_items = self.store.case_handoff_items(cid)
                archived_for_this_case: set[str] = set()

                for item in case_items:
                    if item.state not in {"closed", "accepted"}:
                        continue
                    if before_timestamp is not None and item.updated_at > before_timestamp:
                        continue

                    dt = datetime.fromtimestamp(item.updated_at or current_time_val, tz=timezone.utc)
                    date_bucket = dt.strftime("%Y-%m-%d")
                    bucket_dir = self.archive_dir / date_bucket
                    ensure_private_dir(bucket_dir)

                    dest_path = bucket_dir / f"{item.item_id}.json"
                    write_json_file_atomic(dest_path, item.to_dict())
                    archived_ids.append(item.item_id)
                    archived_for_this_case.add(item.item_id)

                if archived_for_this_case:
                    remaining_items = [
                        it for it in case_items
                        if it.item_id not in archived_for_this_case and it.state != "closed"
                    ]
                    self.store.rewrite_case_handoff_items(cid, remaining_items)

        return archived_ids

    def read_case_entries(self, case_id: str) -> list[HandoffBoardItem]:
        self._require_existing_case(case_id)
        with self.store.handoff_case_lock(case_id):
            self.store.load_case(case_id)
            return self.store.case_handoff_items(case_id)

    def block(
        self,
        *,
        case_id: str,
        item_id: str,
        reviewer_principal: str = "coordinator",
        reason: str = "blocked",
        metadata: dict[str, object] | None = None,
        now: float | None = None,
        frozen_input_version: str = "",
        submission_digest: str = "",
    ) -> HandoffBoardItem:
        return self.decide_acceptance(
            case_id=case_id,
            item_id=item_id,
            verdict="changes_requested",
            reviewer_principal=reviewer_principal,
            reason=reason,
            metadata=metadata,
            now=now,
            frozen_input_version=frozen_input_version,
            submission_digest=submission_digest,
        )

    publish = publish_item
    claim = record_ack
    complete = submit_handoff
    release = decide_acceptance
    cancel = close_item
    read = list_items


HandoffBoard = HandoffBoardService

__all__ = [
    "FrozenInputVersionMismatchError",
    "HandoffBoard",
    "HandoffBoardService",
    "HandoffError",
    "HandoffWriteSetOverlapError",
    "IdempotencyConflictError",
    "InvalidStateTransitionError",
    "compute_payload_digest",
    "is_active_handoff_state",
    "normalize_paths",
    "paths_overlap",
]
