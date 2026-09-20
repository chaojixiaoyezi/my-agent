# LLM: 维护模型提交批次；保持回合锁先于回执锁、批次先提交后投影，修改须核对提交与重试测试。
# 模块用途: 维护模型提交批次。
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import Any

from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from ..runtime_errors import DataCorruptionError
from .models import GuidanceEntry
from .store_guidance_ledger import GuidanceLedger


# LLM: 维护模型提交批次；保持回合锁先于回执锁、批次先提交后投影，修改须核对提交与重试测试。
# 类用途: 维护模型提交批次。
class GuidanceSubmissions:
    # LLM: 批次和回执必须使用同一 ledger，不创建第二份交付状态。
    # 函数用途: 绑定模型请求提交所需的账本能力，初始化没有文件副作用。
    def __init__(self, ledger: GuidanceLedger) -> None:
        self.ledger = ledger
        self.storage = ledger.storage

    # LLM: GuidanceSubmissions：持精确回合锁提交整批权威记录，再逐条投影回执；修改须联测提交中断与相同请求重放。
    # 函数用途: 在真正调用模型前把本批补充消息标记为已开始提交，并返回对应消息 ID。
    def mark_submitted(
        self,
        expected_turn_id: str,
        entries: list[GuidanceEntry] | tuple[GuidanceEntry, ...],
        *,
        attempt_id: str,
        provider_call_id: str = "",
        now: float | None = None,
    ) -> tuple[str, ...]:
        turn_id = str(expected_turn_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        if not turn_id or not normalized_attempt_id:
            raise ValueError("guidance submission requires turn and attempt ids")
        prepared = tuple(
            entry for entry in entries if str(getattr(entry, "guidance_id", "") or "").strip()
        )
        if not prepared:
            return ()
        guidance_ids = tuple(str(entry.guidance_id) for entry in prepared)
        call_id = str(provider_call_id or "").strip()
        if not call_id:
            legacy_source = json.dumps(
                [normalized_attempt_id, *sorted(guidance_ids)],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            call_id = f"legacy-{hashlib.sha256(legacy_source.encode('utf-8')).hexdigest()}"
        submitted_at = now if now is not None else time.time()
        items = [
            {
                "guidance_id": str(entry.guidance_id),
                "dedupe_key": str(
                    (entry.metadata if isinstance(entry.metadata, dict) else {}).get("dedupe_key")
                    or ""
                ).strip(),
            }
            for entry in prepared
        ]
        batch = {
            "schema_version": "conversation_guidance_submission_batch.v1",
            "expected_turn_id": turn_id,
            "attempt_id": normalized_attempt_id,
            "provider_call_id": call_id,
            "items": items,
            "status": "submitted",
            "committed_at": submitted_at,
        }
        with self.ledger.turn_guard(turn_id):
            batch_path = self.storage.guidance_submission_batch_path(turn_id, call_id)
            report = read_json_file_report(
                batch_path,
                context="conversation.guidance_submission_batch.read",
            )
            if report.load_error is not None:
                raise DataCorruptionError("conversation guidance submission batch is unreadable")
            if report.payload:
                stable_keys = (
                    "schema_version",
                    "expected_turn_id",
                    "attempt_id",
                    "provider_call_id",
                    "items",
                )
                if {key: report.payload.get(key) for key in stable_keys} != {
                    key: batch.get(key) for key in stable_keys
                }:
                    raise DataCorruptionError("conversation guidance submission batch conflicts")
                if str(report.payload.get("status") or "") != "submitted":
                    raise DataCorruptionError("rejected guidance submission cannot be replayed")
                batch = dict(report.payload)
            else:
                self._validate_locked(batch, allow_projected=False)
                write_json_file_atomic(batch_path, batch)
            self.apply_locked(batch)
        return guidance_ids

    # LLM: GuidanceSubmissions：仅结构化的模型执行前拒绝允许恢复本次预留；网络结果不明和其它尝试不得沿此路径重放。
    # 函数用途: 模型明确因上下文超限拒绝请求时，把同一批消息恢复为本尝试可重新提交状态。
    def restore_for_retry(
        self,
        expected_turn_id: str,
        entries: list[GuidanceEntry] | tuple[GuidanceEntry, ...],
        *,
        attempt_id: str,
        provider_call_id: str = "",
    ) -> tuple[str, ...]:
        turn_id = str(expected_turn_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        if not turn_id or not normalized_attempt_id:
            raise ValueError("guidance retry restore requires turn and attempt ids")
        call_id = str(provider_call_id or "").strip()
        restored: list[str] = []
        with self.ledger.turn_guard(turn_id):
            if call_id:
                batch_path = self.storage.guidance_submission_batch_path(turn_id, call_id)
                report = read_json_file_report(
                    batch_path,
                    context="conversation.guidance_submission_batch.retry",
                )
                if report.load_error is not None or not report.payload:
                    raise DataCorruptionError("guidance retry submission batch is unavailable")
                batch = dict(report.payload)
                if (
                    str(batch.get("attempt_id") or "") != normalized_attempt_id
                    or str(batch.get("provider_call_id") or "") != call_id
                ):
                    raise DataCorruptionError("guidance retry submission batch mismatch")
                if str(batch.get("status") or "") == "submitted":
                    batch["status"] = "rejected"
                    batch["rejected_at"] = time.time()
                    write_json_file_atomic(batch_path, batch)
                self.apply_locked(batch)
            for entry in entries:
                metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
                dedupe_key = str(metadata.get("dedupe_key") or "").strip()
                if not dedupe_key:
                    continue
                receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
                transition = receipt_path.with_name(f".{receipt_path.name}.transition")
                with locked_file_transition(transition):
                    receipt = self.ledger.read_receipt(receipt_path)
                    receipt_metadata = (
                        receipt.entry.metadata
                        if receipt is not None and isinstance(receipt.entry.metadata, dict)
                        else {}
                    )
                    if (
                        receipt is None
                        or receipt.entry.guidance_id != entry.guidance_id
                        or str(receipt_metadata.get("expected_turn_id") or "").strip()
                        != turn_id
                        or receipt.attempt_id != normalized_attempt_id
                    ):
                        raise DataCorruptionError("guidance retry restore mismatch")
                    if receipt.status == "reserved" and not receipt.submission_id:
                        restored.append(entry.guidance_id)
                        continue
                    if receipt.status != "submitted" or (
                        call_id and receipt.submission_id != call_id
                    ):
                        raise DataCorruptionError("guidance retry restore was not submitted")
                    write_json_file_atomic(
                        receipt_path,
                        replace(
                            receipt,
                            status="reserved",
                            submission_id="",
                            submitted_at=0.0,
                            updated_at=time.time(),
                        ).to_dict(),
                    )
                    restored.append(entry.guidance_id)
        return tuple(restored)

    # LLM: GuidanceSubmissions：调用方持回合锁；先验证全部消息、幂等键与预留身份，再允许提交单个批次文件。
    # 函数用途: 校验一次模型调用对应的补充消息整批身份和当前预留状态。
    def _validate_locked(
        self,
        batch: dict[str, Any],
        *,
        allow_projected: bool,
    ) -> None:
        turn_id = str(batch.get("expected_turn_id") or "").strip()
        attempt_id = str(batch.get("attempt_id") or "").strip()
        call_id = str(batch.get("provider_call_id") or "").strip()
        items = batch.get("items")
        if (
            batch.get("schema_version") != "conversation_guidance_submission_batch.v1"
            or not turn_id
            or not attempt_id
            or not call_id
            or not isinstance(items, list)
            or not items
            or str(batch.get("status") or "") not in {"submitted", "rejected"}
        ):
            raise DataCorruptionError("conversation guidance submission batch is invalid")
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise DataCorruptionError("conversation guidance submission item is invalid")
            guidance_id = str(item.get("guidance_id") or "").strip()
            dedupe_key = str(item.get("dedupe_key") or "").strip()
            if not guidance_id or guidance_id in seen_ids:
                raise DataCorruptionError("conversation guidance submission item id conflicts")
            seen_ids.add(guidance_id)
            if not dedupe_key:
                continue
            if dedupe_key in seen_keys:
                raise DataCorruptionError("conversation guidance submission receipt key conflicts")
            seen_keys.add(dedupe_key)
            receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            with locked_file_transition(transition):
                receipt = self.ledger.read_receipt(receipt_path)
                metadata = (
                    receipt.entry.metadata
                    if receipt is not None and isinstance(receipt.entry.metadata, dict)
                    else {}
                )
                if (
                    receipt is None
                    or receipt.entry.guidance_id != guidance_id
                    or str(metadata.get("expected_turn_id") or "").strip() != turn_id
                    or receipt.attempt_id != attempt_id
                ):
                    raise DataCorruptionError("guidance submission reservation mismatch")
                if not allow_projected and receipt.status != "reserved":
                    raise DataCorruptionError("guidance submission was not reserved")

    # LLM: GuidanceSubmissions：回执是已提交批次的可修复投影；旧批次不得覆盖更新的 submission_id，联测乱序重放。
    # 函数用途: 根据一份已提交或已拒绝的批次幂等修复每条补充消息回执。
    def apply_locked(self, batch: dict[str, Any]) -> None:
        self._validate_locked(batch, allow_projected=True)
        call_id = str(batch.get("provider_call_id") or "").strip()
        batch_status = str(batch.get("status") or "")
        submitted_at = float(batch.get("committed_at") or time.time())
        for item in batch.get("items", []):
            key = str(item.get("dedupe_key") or "").strip()
            if not key:
                continue
            receipt_path = self.storage.guidance_dedupe_path(key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            with locked_file_transition(transition):
                receipt = self.ledger.read_receipt(receipt_path)
                if receipt.status in {"consumed", "rejected"}:
                    continue
                if batch_status == "submitted":
                    if receipt.status == "submitted" and receipt.submission_id != call_id:
                        continue
                    if receipt.status not in {"reserved", "submitted"}:
                        raise DataCorruptionError("guidance submission projection state is invalid")
                    updated = replace(
                        receipt,
                        status="submitted",
                        submission_id=call_id,
                        submitted_at=submitted_at,
                        updated_at=submitted_at,
                    )
                else:
                    if receipt.status == "submitted" and receipt.submission_id != call_id:
                        continue
                    if receipt.status == "reserved" and receipt.submission_id not in {"", call_id}:
                        continue
                    updated = replace(
                        receipt,
                        status="reserved",
                        submission_id="",
                        submitted_at=0.0,
                        updated_at=float(batch.get("rejected_at") or time.time()),
                    )
                if updated != receipt:
                    write_json_file_atomic(receipt_path, updated.to_dict())

    # LLM: GuidanceSubmissions：调用方持回合锁；按原提交次序修批次后才能解释回执，坏批次计数且不影响其它回合。
    # 函数用途: 修复精确回合的模型提交批次投影，并返回发现的损坏批次数。
    def repair_locked(self, turn_id: str) -> int:
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        errors = 0
        batches: list[dict[str, Any]] = []
        for path in sorted((self.storage.guidance_submission_batches_dir / turn_digest).glob("*.json")):
            report = read_json_file_report(
                path,
                context="conversation.guidance_submission_batch.repair",
            )
            payload = report.payload
            if (
                report.load_error is not None
                or payload.get("schema_version")
                != "conversation_guidance_submission_batch.v1"
                or str(payload.get("expected_turn_id") or "") != turn_id
            ):
                errors += 1
                continue
            batches.append(dict(payload))
        batches.sort(
            key=lambda item: (
                float(item.get("committed_at") or 0.0),
                float(item.get("rejected_at") or 0.0),
                str(item.get("provider_call_id") or ""),
            )
        )
        for batch in batches:
            try:
                self.apply_locked(batch)
            except Exception:
                errors += 1
        return errors
