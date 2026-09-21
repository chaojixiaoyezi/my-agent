# LLM: 终态、恢复和网络重试共用旧/新 turn 排序及原回执；预留前先修批次和复读，修改须联测消费、取消与半写重试。
# 模块用途: 收口精确回合消息并恢复未提交消息，避免重试在消费之后重复排队。
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from ..runtime_errors import DataCorruptionError
from .models import GuidanceEntry, normalize_guidance_target_type
from .store_guidance_acknowledgements import GuidanceAcknowledgements
from .store_guidance_ledger import GuidanceLedger
from .store_guidance_records import (
    GuidanceOnceReceipt,
    _legacy_prompt_submission_unknown_receipt,
    _rebound_guidance_receipt,
)
from .store_guidance_submission import GuidanceSubmissions
from .store_io import unlink_quietly


# LLM: 只改绑已确认失效且尚未提交模型的消息；修改须核对回执指纹、索引修复和跨尝试隔离。
# 类用途: 只改绑已确认失效且尚未提交模型的消息。
class GuidanceRebinding:
    # LLM: 逐条改绑只依赖同源账本；外层调用方必须先按顺序持有所有旧、新回合锁。
    # 函数用途: 绑定恢复改绑所需的回执和投影能力，不执行恢复或写盘。
    def __init__(self, ledger: GuidanceLedger) -> None:
        self.ledger = ledger
        self.storage = ledger.storage

    # LLM: GuidanceRebinding：外层已持有所有旧、新回合锁；逐条累计结构化结果，坏回执不能改变其它消息身份。
    # 函数用途: 批量执行已加锁的插话恢复，并把每种结果累计进同一结构化摘要。
    def rebind_entries_locked(
        self,
        entries: list[GuidanceEntry],
        *,
        dead_turn_ids: set[str],
        recovered_turn_id: str,
        summary: dict[str, Any],
    ) -> None:
        for entry in entries:
            outcome = self._safe_rebind_one(
                entry,
                dead_turn_ids=dead_turn_ids,
                recovered_turn_id=recovered_turn_id,
            )
            if outcome == "error":
                summary["errors"] += 1
            elif outcome == "rebound":
                summary["rebound"] += 1
                summary["rebound_guidance_ids"].append(entry.guidance_id)
            elif outcome:
                summary[outcome] += 1

    # LLM: GuidanceRebinding：单条异常折入错误计数；恢复调用方须按错误数关闭失败路径，不能伪造改绑成功。
    # 函数用途: 安全执行单条插话改绑，将异常折成明确的 error 结果供上层统一处理。
    def _safe_rebind_one(
        self,
        entry: GuidanceEntry,
        *,
        dead_turn_ids: set[str],
        recovered_turn_id: str,
    ) -> str:
        try:
            return self._rebind_one(
                entry,
                dead_turn_ids=dead_turn_ids,
                recovered_turn_id=recovered_turn_id,
            )
        except Exception:
            return "error"

    # LLM: GuidanceRebinding：外层持旧、新回合锁，内层锁定回执；仅尚未提交且属于失效尝试的消息可写新绑定及索引。
    # 函数用途: 原子处理一条待恢复插话，区分安全改绑、旧旁路投递不明和已提交不明三种结果。
    def _rebind_one(
        self,
        entry: GuidanceEntry,
        *,
        dead_turn_ids: set[str],
        recovered_turn_id: str,
    ) -> str:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return ""
        receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self.ledger.read_receipt(receipt_path)
            if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
                raise DataCorruptionError("conversation guidance receipt entry mismatch")
            receipt_metadata = (
                dict(receipt.entry.metadata)
                if isinstance(receipt.entry.metadata, dict)
                else {}
            )
            old_turn_id = str(receipt_metadata.get("expected_turn_id") or "").strip()
            if old_turn_id not in dead_turn_ids:
                return ""
            if receipt.status == "submitted":
                return (
                    "legacy_submission_unknown"
                    if receipt.migration.get("legacy_runner_prompt_delivery")
                    else "submitted_unknown"
                )
            if receipt.status not in {"pending", "reserved"}:
                return ""
            if receipt.status == "reserved" and receipt.attempt_id != old_turn_id:
                raise DataCorruptionError("reserved guidance attempt does not match dead turn")
            delivered_at = float(
                self.ledger.read_delivered().get(receipt.entry.guidance_id) or 0.0
            )
            if delivered_at > 0:
                migrated = _legacy_prompt_submission_unknown_receipt(
                    receipt,
                    old_turn_id=old_turn_id,
                    delivered_at=delivered_at,
                )
                write_json_file_atomic(receipt_path, migrated.to_dict())
                return "legacy_submission_unknown"
            self.write_rebound_locked(receipt, recovered_turn_id=recovered_turn_id)
            return "rebound"

    # LLM: 调用者已持有全部旧/新 turn 与原 receipt 锁，并证明未提交；先写权威回执，索引失败可从回执修复。
    # 函数用途: 共用一次消息改绑的持久写入顺序，供恢复和网络重试接续使用。
    def write_rebound_locked(
        self, receipt: GuidanceOnceReceipt, *, recovered_turn_id: str,
    ) -> GuidanceOnceReceipt:
        old_turn_id = str(receipt.entry.metadata.get("expected_turn_id") or "")
        rebound = _rebound_guidance_receipt(
            receipt, old_turn_id=old_turn_id, recovered_turn_id=recovered_turn_id,
        )
        write_json_file_atomic(self.storage.guidance_dedupe_path(receipt.dedupe_key), rebound.to_dict())
        self.ledger.ensure_turn_index(rebound)
        self.ledger.rebind_input_index(rebound, old_turn_id=old_turn_id)
        unlink_quietly(self.storage.guidance_turn_index_path(old_turn_id, receipt.dedupe_key))
        return rebound


# LLM: 恢复可接续确认死亡轮的 reserved，网络重试仅接 fresh pending；两者保持独立资格、同一回执与写入顺序。
# 类用途: 协调消息终态、失效回合恢复和网络重试中的准确接续。
class GuidanceRecovery:
    # LLM: 终态先修提交再修确认；队列读取能力显式提供，不读取整个 Store 或猜测尝试身份。
    # 函数用途: 组装结束、释放与恢复操作所需的同源组件，初始化不改变持久状态。
    def __init__(
        self, ledger: GuidanceLedger, *, submissions: GuidanceSubmissions,
        acknowledgements: GuidanceAcknowledgements,
        recent_report: Callable[..., tuple[list[GuidanceEntry], list[dict[str, Any]]]],
    ) -> None:
        self.ledger = ledger
        self.storage = ledger.storage
        self.submissions = submissions
        self.acknowledgements = acknowledgements
        self._recent_report = recent_report
        self._rebinding = GuidanceRebinding(ledger)

    # LLM: GuidanceRecovery：终态与运行循环共用回合锁；先修已提交批次，只结算该回合索引，联测取消与确认竞争。
    # 函数用途: 回合结束时原子拒绝尚未消费的补充消息，并返回本回合状态计数。
    def reject_pending(
        self,
        expected_turn_id: str,
        *,
        reject_reserved: bool = False,
    ) -> dict[str, int]:
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            return {
                "rejected": 0,
                "reserved": 0,
                "submitted": 0,
                "consumed": 0,
                "retired_legacy": 0,
                "errors": 0,
            }
        with self.ledger.turn_guard(turn_id):
            return self._reject_pending_locked(
                turn_id,
                reject_reserved=reject_reserved,
            )

    # LLM: GuidanceRecovery：调用方已证明尝试死亡；提交结果未知始终保留，只有尚未越过模型提交边界的预留可释放。
    # 函数用途: 在请求租约已确认失效后，把尚未开始模型提交的预留消息恢复为可再次认领。
    def release_reserved(
        self,
        expected_turn_id: str,
        *,
        dead_attempt_id: str = "",
    ) -> dict[str, int]:
        turn_id = str(expected_turn_id or "").strip()
        summary: dict[str, Any] = {
            "released": 0,
            "released_guidance_ids": [],
            "submitted": 0,
            "errors": 0,
        }
        if not turn_id:
            return summary
        expected_attempt = str(dead_attempt_id or "").strip()
        with self.ledger.turn_guard(turn_id):
            summary["errors"] += self.submissions.repair_locked(
                turn_id
            )
            turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
            index_dir = self.storage.guidance_turn_index_dir / turn_digest
            for index_path in sorted(index_dir.glob("*.json")):
                self._release_reserved_index_locked(
                    index_path, turn_id=turn_id, expected_attempt=expected_attempt, summary=summary,
                )
        return summary

    # LLM: 候选新轮尚未落盘；按排序取得旧/新 turn，修原提交/确认，再持 receipt 复读。reserve_turn 只准预留准确身份，不得启动 runner。
    # 函数用途: 网络重试仅为仍待处理的同一消息准备接续，避免旧 pending 快照在消费后多排执行轮。
    def prepare_pending_replay(
        self, dedupe_key: str, *, expected_turn_id: str, recovered_turn_id: str,
        reserve_turn: Callable[[], None],
    ) -> tuple[GuidanceOnceReceipt, bool]:
        if not dedupe_key or not expected_turn_id or not recovered_turn_id:
            raise ValueError("guidance replay requires receipt and exact turn ids")
        with ExitStack() as stack:
            for turn_id in sorted({expected_turn_id, recovered_turn_id}):
                stack.enter_context(self.ledger.turn_guard(turn_id))
            errors = self.submissions.repair_locked(expected_turn_id)
            errors += self.acknowledgements.repair_locked(expected_turn_id)
            if errors:
                raise DataCorruptionError("guidance replay batch repair is incomplete")
            receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
            with locked_file_transition(receipt_path.with_name(f".{receipt_path.name}.transition")):
                receipt = self.ledger.read_receipt(receipt_path)
                if receipt is None or receipt.dedupe_key != dedupe_key:
                    raise DataCorruptionError("guidance replay receipt is unavailable")
                if receipt.status != "pending":
                    return receipt, False
                if receipt.entry.metadata.get("expected_turn_id") != expected_turn_id:
                    raise DataCorruptionError("guidance replay binding changed")
                reserve_turn()
                if recovered_turn_id != expected_turn_id:
                    receipt = self._rebinding.write_rebound_locked(
                        receipt, recovered_turn_id=recovered_turn_id,
                    )
                return receipt, True

    # LLM: GuidanceRecovery 调用方持回合锁；本函数逐条锁回执并累计结果，坏索引不能释放别的尝试，联测部分提交恢复。
    # 函数用途: 结算一个回合索引对应的预留消息；只释放尚未提交且匹配失效尝试的回执。
    def _release_reserved_index_locked(
        self, index_path: Path, *, turn_id: str, expected_attempt: str,
        summary: dict[str, Any],
    ) -> None:
        index_report = read_json_file_report(
            index_path,
            context="conversation.guidance_turn_index.release",
        )
        dedupe_key = str(index_report.payload.get("dedupe_key") or "").strip()
        if index_report.load_error is not None or not dedupe_key:
            summary["errors"] += 1
            return
        receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        try:
            with locked_file_transition(transition):
                receipt = self.ledger.read_receipt(receipt_path)
                metadata = (
                    receipt.entry.metadata
                    if receipt is not None and isinstance(receipt.entry.metadata, dict)
                    else {}
                )
                if (
                    receipt is None
                    or str(metadata.get("expected_turn_id") or "").strip() != turn_id
                ):
                    summary["errors"] += 1
                    return
                if receipt.status == "submitted":
                    summary["submitted"] += 1
                    return
                if receipt.status != "reserved" or (
                    expected_attempt and receipt.attempt_id != expected_attempt
                ):
                    return
                write_json_file_atomic(
                    receipt_path,
                    replace(
                        receipt,
                        status="pending",
                        attempt_id="",
                        submitted_at=0.0,
                        updated_at=time.time(),
                    ).to_dict(),
                )
                summary["released"] += 1
                summary["released_guidance_ids"].append(receipt.entry.guidance_id)
        except Exception:
            summary["errors"] += 1


    # LLM: GuidanceRecovery：旧尝试身份由运行账本提供；先按排序锁全部相关回合再修提交批次，不从消息正文推断归属。
    # 函数用途: 子代理崩溃重启时，把尚未提交模型的插话安全改绑到新 attempt，避免消息永久卡住或重复执行。
    def rebind_unsubmitted(
        self,
        target_type: str,
        target_id: str,
        *,
        dead_turn_ids: list[str] | tuple[str, ...] | set[str],
        recovered_turn_id: str,
    ) -> dict[str, Any]:
        normalized_type = normalize_guidance_target_type(target_type)
        normalized_target = str(target_id or "").strip()
        recovered = str(recovered_turn_id or "").strip()
        dead = {
            str(item or "").strip()
            for item in dead_turn_ids
            if str(item or "").strip() and str(item or "").strip() != recovered
        }
        summary: dict[str, Any] = {
            "rebound": 0,
            "rebound_guidance_ids": [],
            "legacy_submission_unknown": 0,
            "submitted_unknown": 0,
            "errors": 0,
        }
        if not normalized_type or not normalized_target or not recovered or not dead:
            return summary
        entries, load_errors = self._recent_report(
            normalized_type,
            normalized_target,
            limit=0,
            include_delivered=True,
        )
        summary["errors"] += len(load_errors)
        with ExitStack() as stack:
            for turn_id in sorted({*dead, recovered}):
                stack.enter_context(self.ledger.turn_guard(turn_id))
            for turn_id in sorted(dead):
                summary["errors"] += self.submissions.repair_locked(
                    turn_id
                )
            self._rebinding.rebind_entries_locked(
                entries,
                dead_turn_ids=dead,
                recovered_turn_id=recovered,
                summary=summary,
            )
        return summary

    # LLM: GuidanceRecovery：调用方持回合锁；先修提交再修确认，内部无幂等消息按送达投影退休，保持原提交顺序。
    # 函数用途: 在已持有回合锁时逐条结算当前回合索引。
    def _reject_pending_locked(
        self,
        turn_id: str,
        *,
        reject_reserved: bool = False,
    ) -> dict[str, int]:
        summary = {
            "rejected": 0,
            "reserved": 0,
            "submitted": 0,
            "consumed": 0,
            "retired_legacy": 0,
            "errors": 0,
        }
        summary["errors"] += self.submissions.repair_locked(
            turn_id
        )
        summary["errors"] += self.acknowledgements.repair_locked(turn_id)
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        index_dir = self.storage.guidance_turn_index_dir / turn_digest
        for index_path in sorted(index_dir.glob("*.json")):
            index_report = read_json_file_report(
                index_path,
                context="conversation.guidance_turn_index.read",
            )
            dedupe_key = str(index_report.payload.get("dedupe_key") or "").strip()
            if index_report.load_error is not None or not dedupe_key:
                summary["errors"] += 1
                continue
            receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            try:
                with locked_file_transition(transition):
                    receipt = self.ledger.read_receipt(receipt_path)
                    if receipt is None:
                        summary["errors"] += 1
                        continue
                    metadata = (
                        receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
                    )
                    if str(metadata.get("expected_turn_id") or "").strip() != turn_id:
                        summary["errors"] += 1
                        continue
                    # Gateway terminalization holds its outer exact-turn lock. A reserved row is
                    # then proven not to have an atomic submission batch and is safe to reject.
                    if receipt.status == "pending" or (
                        reject_reserved and receipt.status == "reserved"
                    ):
                        receipt = replace(
                            receipt,
                            status="rejected",
                            submission_id="",
                            updated_at=time.time(),
                        )
                        write_json_file_atomic(receipt_path, receipt.to_dict())
                    if receipt.status in summary:
                        summary[receipt.status] += 1
            except Exception:
                summary["errors"] += 1
        # Internal non-idempotent request guidance has no receipt state. The
        # delivered projection is its only durable retirement fact, so close it
        # under the same exact-turn guard instead of letting stop replay it.
        entries, load_errors = self._recent_report(
            "request",
            turn_id,
            limit=0,
            include_delivered=False,
        )
        legacy_ids = tuple(
            entry.guidance_id
            for entry in entries
            if not str(
                (entry.metadata if isinstance(entry.metadata, dict) else {}).get("dedupe_key")
                or ""
            ).strip()
        )
        if legacy_ids:
            self.ledger.mark_delivered(legacy_ids)
            summary["retired_legacy"] += len(legacy_ids)
        summary["errors"] += len(load_errors)
        return summary

    # LLM: GuidanceRecovery：只释放尚未认领的回执；在 migration 记录旧回合，不改参与指纹的 entry，也不释放已提交消息。
    # 函数用途: 回合结束时释放仍未被任何轮次认领的补充消息，使其可被后续轮次接手。
    def release_unclaimed(self, turn_id: str) -> int:
        normalized = str(turn_id or "").strip()
        if not normalized:
            return 0
        bucket = self.storage.guidance_turn_index_dir / hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
        if not bucket.is_dir():
            return 0
        released = 0
        for path in sorted(bucket.glob("*.json")):
            try:
                report = read_json_file_report(
                    path, context="conversation.guidance_turn_index.read"
                )
                payload = report.payload
                if report.load_error is not None or not payload:
                    continue
                dedupe_key = str(payload.get("dedupe_key") or "").strip()
                if not dedupe_key or str(payload.get("expected_turn_id") or "") != normalized:
                    continue
                receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
                transition = receipt_path.with_name(f".{receipt_path.name}.transition")
                with locked_file_transition(transition):
                    receipt = self.ledger.read_receipt(receipt_path)
                    if receipt is None or receipt.status != "pending":
                        continue
                    metadata = (
                        receipt.entry.metadata
                        if isinstance(receipt.entry.metadata, dict)
                        else {}
                    )
                    if str(metadata.get("expected_turn_id") or "").strip() != normalized:
                        continue
                    # entry.metadata 参与回执行/投影一致性校验与正文指纹，这里一个字都不改；
                    # 释放只记在回执自己的 migration 上（receipt 级字段，不进 entry）。
                    migration = dict(receipt.migration)
                    released_ids = list(migration.get("released_turn_ids") or [])
                    if normalized not in released_ids:
                        released_ids.append(normalized)
                    migration["released_turn_ids"] = released_ids[-20:]
                    updated = replace(
                        receipt,
                        updated_at=time.time(),
                        migration=migration,
                    )
                    write_json_file_atomic(receipt_path, updated.to_dict())
                    released += 1
                unlink_quietly(path)
            except Exception:
                # 释放是兜底恢复动作：单条损坏不能打断回合收口，也不能影响其他插话。
                continue
        return released
