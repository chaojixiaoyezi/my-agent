# LLM: 终态、恢复和网络重试共用旧/新 turn 排序及原回执；预留前先修批次和复读，修改须联测消费、取消与半写重试。
# 模块用途: 收口精确回合消息并恢复未提交消息，避免重试在消费之后重复排队。
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from ..runtime_errors import DataCorruptionError
from .message_scan import find_message_dedupe
from .models import GuidanceEntry, normalize_guidance_target_type
from .session_messaging import (
    SESSION_MESSAGE_ORIGIN_KIND,
    SESSION_MESSAGE_RELEASE_LIMIT,
    SESSION_MESSAGE_RELEASE_LIMIT_REACHED,
    SESSION_TASK_ORIGIN_KIND,
)
from .store_guidance_acknowledgements import GuidanceAcknowledgements, transcript_dedupe_key
from .store_guidance_ledger import GuidanceLedger
from .store_guidance_records import (
    GuidanceOnceReceipt,
    _legacy_prompt_submission_unknown_receipt,
    _rebound_guidance_receipt,
)
from .store_guidance_submission import GuidanceSubmissions
from .store_io import unlink_quietly
from .wake_poison import WAKE_VERDICT_FAILURE, verdict_for_error


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
    #   release_task_body 只由后台片异常结束且任务没被取消时传 True（派活正文退回给同一任务号的重跑），前台终态保持默认。
    #   failure 是这一回合没正常结束的那个异常（前台终态与后台收尾都传；没有异常传 None）：只用来决定这次释放计不计次
    #   （failure_counts_toward_release_limit），不改变释放与否。返回计数项见 TURN_END_SUMMARY_KEYS；续跑上限收口改走 settle_dead_turn。
    # 函数用途: 回合结束时原子拒绝尚未消费的补充消息，并返回本回合状态计数。
    def reject_pending(
        self,
        expected_turn_id: str,
        *,
        reject_reserved: bool = False,
        release_task_body: bool = False,
        failure: BaseException | None = None,
    ) -> dict[str, int]:
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            return dict.fromkeys(TURN_END_SUMMARY_KEYS, 0)
        with self.ledger.turn_guard(turn_id):
            return self._reject_pending_locked(
                turn_id,
                TurnEndSettlement(reject_reserved, release_task_body, failure_counts_toward_release_limit(failure)),
            )

    # LLM: GuidanceRecovery：只给续跑上限收口用（Gateway 启动恢复已证明提交插话的进程死亡）。等于 reject_pending(reject_reserved=True)
    #   再加上：确认批次修复后仍停在 submitted 的插话也收成终态（settle_dead_submission），用户的插话不会悄悄悬着、也不会送两遍。
    #   副作用同 reject_pending：持回合锁写回执；返回 TURN_END_SUMMARY_KEYS 各项计数。
    # 函数用途: 续跑上限收口时结算本回合全部插话，连随进程死掉的已提交插话一起定终态。
    def settle_dead_turn(self, expected_turn_id: str) -> dict[str, int]:
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            return dict.fromkeys(TURN_END_SUMMARY_KEYS, 0)
        with self.ledger.turn_guard(turn_id):
            return self._reject_pending_locked(turn_id, TurnEndSettlement(reject_reserved=True, settle_dead_submissions=True))

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
    def _reject_pending_locked(self, turn_id: str, settlement: TurnEndSettlement) -> dict[str, int]:
        summary = dict.fromkeys(TURN_END_SUMMARY_KEYS, 0)
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
            try:
                outcomes = self._settle_turn_receipt(dedupe_key, turn_id, settlement)
            except Exception:
                outcomes = ("errors",)
            if "released" in outcomes:
                # 已释放给下一回合：这一回合的索引不再指向它，下一回合认领时会绑到自己名下。
                unlink_quietly(index_path)
            _count_outcomes(summary, outcomes)
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

    # LLM: GuidanceRecovery：调用方持回合锁；这里再持回执锁，只处理绑定在本回合上的回执。pending/已预留（reject_reserved）的
    #   按 settle_unconsumed_receipt 收尾（会话消息释放；派活正文在 release_task_body 时退回给同一回合号；其余 rejected）。
    #   已提交的只有 settle_dead_submissions（续跑上限收口：提交它的进程已证明死亡，确认批次已先修复）时才收口，见
    #   settle_dead_submission；否则和已消费的一样原样计数。返回用于汇总计数的结果名元组（turn_end_labels）："released" 表示
    #   已退回 pending（调用方删本回合索引，再认领时补回），"errors" 表示回执缺失或绑定对不上。副作用：写回执。
    # 函数用途: 回合收尾时结算本回合索引里的一条补充消息回执。
    def _settle_turn_receipt(self, dedupe_key: str, turn_id: str, settlement: TurnEndSettlement) -> tuple[str, ...]:
        receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
        with locked_file_transition(receipt_path.with_name(f".{receipt_path.name}.transition")):
            receipt = self.ledger.read_receipt(receipt_path)
            metadata = receipt.entry.metadata if receipt is not None and isinstance(receipt.entry.metadata, dict) else {}
            if receipt is None or str(metadata.get("expected_turn_id") or "").strip() != turn_id:
                return ("errors",)
            settled = _turn_end_settled(receipt, turn_id, settlement, self.storage)
            if settled is None:
                return (receipt.status,)
            write_json_file_atomic(receipt_path, settled.to_dict())
        return turn_end_labels(settled, dead=settlement.settle_dead_submissions and receipt.status == "submitted")

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


# LLM: 回合没消费就结束（/stop、报错、崩溃）时，pending/已预留回执的唯一收尾规则：插话等普通回执照旧转 rejected
#   （Gateway 输入对账会把被拒插话重新排成请求）；会话消息改为释放回 pending，在回执自己的 migration.released_turn_ids 记下
#   这一回合，下一回合按跨回合例外正式认领（认领时改绑到那一回合）——否则回执永久 rejected，内容只靠被停回合留在历史里的
#   那段输入碰巧可见（2026-09-29 真实链路实测）。释放按次计数（migration.release_count），不按回合去重：同一条唤醒重跑
#   用同一个回合号，按回合去重时它反复失败永远到不了上限。只有会让回合崩溃的失败才计次（settlement.count_release，
#   判据见 failure_counts_toward_release_limit）：计次的失败在已计满 SESSION_MESSAGE_RELEASE_LIMIT 次后再发生时转 rejected，
#   migration.rejection_code 记 SESSION_MESSAGE_RELEASE_LIMIT_REACHED，防止一条会让回合崩溃的消息无限循环；不计次的结束
#   （超时、429、连接、环境故障、/stop、取消、正常结束）只释放，不计次也不会被拒，瞬时和环境故障不丢消息。
#   已提交（submitted）的回执调用方不会传进来：提交结果未知，释放会重复消费。派活正文默认不释放：任务被取消后释放会让
#   前台回合认领到已取消任务的正文；只有后台派活片非取消的失败（调用方按结构化任务状态判定后传 release_task_body）才退回
#   pending，且不记 released_turn_ids、不授权跨回合，只有同一回合号（即同一 session_task_id）的重跑能认领，次数与上限同上。
#   只改回执级字段，entry.metadata 不动（参与正文指纹）。纯函数，不写盘。
# 函数用途: 决定一条没被消费的回执在回合结束时是释放（退回 pending），还是转 rejected。
def settle_unconsumed_receipt(
    receipt: GuidanceOnceReceipt, turn_id: str, settlement: TurnEndSettlement, *, now: float,
) -> GuidanceOnceReceipt:
    metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
    origin_kind = str(metadata.get("origin_kind") or "").strip()
    if origin_kind == SESSION_MESSAGE_ORIGIN_KIND:
        return _release_or_reject(receipt, settlement, now=now, authorized_turn_id=turn_id)
    if origin_kind == SESSION_TASK_ORIGIN_KIND and settlement.release_task_body:
        return _release_or_reject(receipt, settlement, now=now, authorized_turn_id="")
    return replace(receipt, status="rejected", submission_id="", updated_at=now)


# LLM: settle_unconsumed_receipt 的释放分支：只有 settlement.count_release 时才计次（migration.release_count 加一），计次的失败在
#   已计满 SESSION_MESSAGE_RELEASE_LIMIT 次后再发生时转 rejected 带码；不计次时只释放、不改次数、也不会被拒。authorized_turn_id
#   非空时把它记进 released_turn_ids，授权别的回合跨回合认领（会话消息），为空时不授权（派活正文）。纯函数。
# 函数用途: 把一条没消费的回执退回 pending（该计次时计一次），计次的失败次数用满时改为 rejected。
def _release_or_reject(
    receipt: GuidanceOnceReceipt, settlement: TurnEndSettlement, *, now: float, authorized_turn_id: str,
) -> GuidanceOnceReceipt:
    migration = dict(receipt.migration)
    release_count = migration.get("release_count")
    release_count = release_count if type(release_count) is int and release_count > 0 else 0
    if settlement.count_release and release_count >= SESSION_MESSAGE_RELEASE_LIMIT:
        migration["rejection_code"] = SESSION_MESSAGE_RELEASE_LIMIT_REACHED
        return replace(receipt, status="rejected", submission_id="", updated_at=now, migration=migration)
    if authorized_turn_id:
        released = [str(item) for item in (migration.get("released_turn_ids") or []) if str(item or "").strip()]
        if authorized_turn_id not in released:
            released.append(authorized_turn_id)
        migration["released_turn_ids"] = released
    if settlement.count_release:
        migration["release_count"] = release_count + 1
    return replace(receipt, status="pending", attempt_id="", submission_id="", submitted_at=0.0,
                   updated_at=now, migration=migration)


# LLM: 回合结束时收尾一条回执要用的四个结构化开关：reject_reserved（持回合锁的终态可以收已预留的回执）、release_task_body
#   （后台片非取消的失败时派活正文退回给同一任务号）、count_release（这次没消费就结束计不计入释放上限）、
#   settle_dead_submissions（只有续跑上限收口传 True：提交它的进程已证明死亡，已提交未确认的插话也要收成终态）。
#   只由 reject_pending 构造。
# 类用途: 一次回合收尾对补充消息回执的处理方式。
@dataclass(frozen=True)
class TurnEndSettlement:
    reject_reserved: bool = False
    release_task_body: bool = False
    count_release: bool = False
    settle_dead_submissions: bool = False


# reject_pending 返回的计数项：原有各终态计数，加上续跑上限收口时的 dead_submissions（收口的已提交未确认插话）、
# recorded_in_transcript（其中历史里已有、记成已消费的）、backup_turns（拒收后会由入口回执排成“备用下一轮”的）。
TURN_END_SUMMARY_KEYS = ("rejected", "released", "reserved", "submitted", "consumed", "retired_legacy", "errors",
                         "dead_submissions", "recorded_in_transcript", "backup_turns")


# LLM: 只认结构化事实：插话要写进历史（record_in_transcript）、有会话和编号，且会话历史里已有它的幂等键
#   （transcript_dedupe_key，和确认后写历史用的同一个）。读历史文件只比去重键，不读正文；历史读不出来时抛错，由调用方按坏账处理、
#   不改回执（宁可留着也不重复送达）。
# 函数用途: 判断一条插话的内容是不是已经写进了会话历史。
def _recorded_in_transcript(storage: object, entry: GuidanceEntry) -> bool:
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    thread_id = str(metadata.get("thread_id") or "").strip()
    guidance_id = str(entry.guidance_id or "").strip()
    if metadata.get("record_in_transcript") is not True or not thread_id or not guidance_id:
        return False
    path = storage.message_path(thread_id)
    return path.exists() and find_message_dedupe(path, transcript_dedupe_key(guidance_id)) is not None


# LLM: 续跑上限收口（settle_dead_submissions）时对“已提交、确认批次已修复仍没有、提交它的进程已死”的插话：
#   历史里已有它（recorded）就收成 consumed，migration.settle_reason=recorded_in_transcript，不再起备用下一轮（否则模型看到两遍）；
#   历史里没有，就和没被取走的一样按 settle_unconsumed_receipt 收尾（TUI/IM 插话拒收 → 入口回执排成备用下一轮）。
#   两种都在 migration.dead_submission 记下死掉的那次提交（提交编号、认领尝试）。纯函数，不写盘。
# 函数用途: 给一条随进程死掉的已提交插话定终态：已在历史里就算已消费，不在就按未消费拒收或释放。
def settle_dead_submission(
    receipt: GuidanceOnceReceipt, turn_id: str, settlement: TurnEndSettlement, *, recorded: bool,
) -> GuidanceOnceReceipt:
    now = time.time()
    migration = {**receipt.migration,
                 "dead_submission": {"submission_id": receipt.submission_id, "attempt_id": receipt.attempt_id}}
    if recorded:
        return replace(receipt, status="consumed", updated_at=now,
                       migration={**migration, "settle_reason": "recorded_in_transcript"})
    return settle_unconsumed_receipt(replace(receipt, migration=migration), turn_id, settlement, now=now)


# LLM: 回合收尾对一条回执的处置规则：续跑上限收口时已提交的走 settle_dead_submission（先查历史里有没有它）；pending，以及
#   reject_reserved 时的 reserved（Gateway 终态持回合锁，证明它没有原子提交批次）走 settle_unconsumed_receipt；其余返回 None，
#   调用方原样计数。会读会话历史的去重键（_recorded_in_transcript），不写盘。
# 函数用途: 决定一条回执在回合收尾时要不要改、改成什么。
def _turn_end_settled(
    receipt: GuidanceOnceReceipt, turn_id: str, settlement: TurnEndSettlement, storage: object,
) -> GuidanceOnceReceipt | None:
    if settlement.settle_dead_submissions and receipt.status == "submitted":
        return settle_dead_submission(receipt, turn_id, settlement,
                                      recorded=_recorded_in_transcript(storage, receipt.entry))
    if receipt.status == "pending" or (settlement.reject_reserved and receipt.status == "reserved"):
        return settle_unconsumed_receipt(receipt, turn_id, settlement, now=time.time())
    return None


# 函数用途: 把一条回执的收口结果计数累加进 reject_pending 的汇总，只认汇总里已有的计数名。
def _count_outcomes(summary: dict[str, int], outcomes: tuple[str, ...]) -> None:
    for outcome in outcomes:
        if outcome in summary:
            summary[outcome] += 1


# LLM: 计数名只由结构化终态推出：退回 pending 记 released，其余记终态名；随进程死掉的已提交插话另记 dead_submissions，
#   其中按已在历史记成 consumed 的再记 recorded_in_transcript；拒收且带入口请求号（gateway_input_request_id）的另记 backup_turns，
#   因为入口对账会把它排成备用下一轮。
# 函数用途: 把一条回执的收口结果翻成 reject_pending 汇总里的计数名。
def turn_end_labels(settled: GuidanceOnceReceipt, *, dead: bool) -> tuple[str, ...]:
    labels = ["released" if settled.status == "pending" else settled.status]
    if dead:
        labels.append("dead_submissions")
        if settled.status == "consumed":
            labels.append("recorded_in_transcript")
    metadata = settled.entry.metadata if isinstance(settled.entry.metadata, dict) else {}
    if settled.status == "rejected" and str(metadata.get("gateway_input_request_id") or "").strip():
        labels.append("backup_turns")
    return tuple(labels)


# LLM: 回合没消费就结束时这次释放计不计次，唯一判据是唤醒毒丸的错误分类 wake_poison.verdict_for_error（两层同一个权威，
#   3a 2026-09-29 裁定）：只有毒丸会计数的失败（程序错误、未分类异常等会让回合崩溃的）计次；瞬时（超时、429、连接）、环境级
#   （401/402/403/404/407、配置、额度用完）、用户 /stop 与取消（中断类）不计次；没有异常（正常结束、宿主收尾、恢复）也不计次。
# 函数用途: 判断一次回合失败是否计入会话消息与派活正文的释放上限。
def failure_counts_toward_release_limit(failure: BaseException | None) -> bool:
    return failure is not None and verdict_for_error(failure).kind == WAKE_VERDICT_FAILURE
