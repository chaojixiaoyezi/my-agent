# LLM: 维护模型确认批次及历史投影；修改须核对确认重放、消息去重和终态恢复测试。
# 模块用途: 维护模型确认批次及历史投影。
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from ..runtime_errors import DataCorruptionError
from .models import GuidanceEntry, MessageLogEntry
from .store_guidance_ledger import GuidanceLedger
from .store_guidance_submission import GuidanceSubmissions


# LLM: 维护模型确认批次及历史投影；修改须核对确认重放、消息去重和终态恢复测试。
# 类用途: 维护模型确认批次及历史投影。
class GuidanceAcknowledgements:
    # LLM: 只接收同源 ledger、提交批次及消息幂等写入；不依赖整个 ConversationStore。
    # 函数用途: 绑定模型确认与消息投影能力，初始化不消费任何消息。
    def __init__(
        self, ledger: GuidanceLedger, *, submissions: GuidanceSubmissions,
        append_message_once: Callable[..., MessageLogEntry],
    ) -> None:
        self.ledger = ledger
        self.storage = ledger.storage
        self.submissions = submissions
        self._append_message_once = append_message_once

    # LLM: GuidanceAcknowledgements：模型成功后先原子提交确认批次，再修回执和消息投影；精确回合及 provider 身份不可省略。
    # 函数用途: 模型确认收到整批补充消息后，原子提交批次并把每条回执补齐为已消费。
    def consume_submitted(
        self,
        expected_turn_id: str,
        entries: list[GuidanceEntry] | tuple[GuidanceEntry, ...],
        *,
        provider_call_id: str = "",
        now: float | None = None,
    ) -> tuple[str, ...]:
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            raise ValueError("guidance provider ack requires expected_turn_id")
        prepared = tuple(
            entry for entry in entries if str(getattr(entry, "guidance_id", "") or "").strip()
        )
        if not prepared:
            return ()
        guidance_ids = tuple(str(entry.guidance_id) for entry in prepared)
        items: list[dict[str, str]] = []
        for entry in prepared:
            metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
            receipt_turn = str(metadata.get("expected_turn_id") or "").strip()
            if receipt_turn and receipt_turn != turn_id:
                raise DataCorruptionError("guidance provider ack turn mismatch")
            key = str(metadata.get("dedupe_key") or "").strip()
            items.append({"guidance_id": str(entry.guidance_id), "dedupe_key": key})
        committed_at = now if now is not None else time.time()
        call_id = str(provider_call_id or "").strip()
        batch = {
            "schema_version": "conversation_guidance_ack_batch.v3",
            "expected_turn_id": turn_id,
            "items": items,
            "provider_call_id": call_id,
            "committed_at": committed_at,
        }
        with self.ledger.turn_guard(turn_id):
            if call_id:
                submission_path = self.storage.guidance_submission_batch_path(turn_id, call_id)
                submission_report = read_json_file_report(
                    submission_path,
                    context="conversation.guidance_submission_batch.ack",
                )
                submission = submission_report.payload
                if (
                    submission_report.load_error is not None
                    or not submission
                    or str(submission.get("status") or "") != "submitted"
                    or str(submission.get("provider_call_id") or "") != call_id
                ):
                    raise DataCorruptionError("guidance provider ack has no submitted batch")
                self.submissions.apply_locked(submission)
            batch_path = self.storage.guidance_ack_batch_path(turn_id, guidance_ids)
            report = read_json_file_report(batch_path, context="conversation.guidance_ack_batch.read")
            if report.load_error is not None:
                raise DataCorruptionError("conversation guidance ack batch is unreadable")
            if report.payload:
                stable_existing = {
                    key: report.payload.get(key)
                    for key in (
                        "schema_version",
                        "expected_turn_id",
                        "items",
                        "provider_call_id",
                    )
                }
                stable_batch = {key: batch.get(key) for key in stable_existing}
                if stable_existing != stable_batch:
                    raise DataCorruptionError("conversation guidance ack batch conflicts")
                batch = dict(report.payload)
                committed_at = float(batch.get("committed_at") or committed_at)
            else:
                self._validate_locked(batch)
                write_json_file_atomic(batch_path, batch)
            self._apply_locked(batch)
        self.ledger.mark_delivered(guidance_ids, now=committed_at)
        return guidance_ids

    # LLM: GuidanceAcknowledgements：调用方持回合锁；逐条核对消息、幂等键和已提交状态，禁止提交一份不可修复的确认批次。
    # 函数用途: 校验模型确认批次中的每条消息、回执键和精确回合完全对应。
    def _validate_locked(self, batch: dict[str, Any]) -> None:
        turn_id = str(batch.get("expected_turn_id") or "").strip()
        items = batch.get("items")
        if (
            batch.get("schema_version")
            not in {"conversation_guidance_ack_batch.v2", "conversation_guidance_ack_batch.v3"}
            or not turn_id
            or not isinstance(items, list)
            or not items
        ):
            raise DataCorruptionError("conversation guidance ack batch is invalid")
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        provider_call_id = str(batch.get("provider_call_id") or "").strip()
        for item in items:
            if not isinstance(item, dict):
                raise DataCorruptionError("conversation guidance ack item is invalid")
            guidance_id = str(item.get("guidance_id") or "").strip()
            dedupe_key = str(item.get("dedupe_key") or "").strip()
            if not guidance_id or guidance_id in seen_ids:
                raise DataCorruptionError("conversation guidance ack item id conflicts")
            seen_ids.add(guidance_id)
            if not dedupe_key:
                continue
            if dedupe_key in seen_keys:
                raise DataCorruptionError("conversation guidance ack receipt key conflicts")
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
                    or receipt.status not in {"submitted", "consumed"}
                    or (
                        provider_call_id
                        and receipt.status == "submitted"
                        and receipt.submission_id != provider_call_id
                    )
                ):
                    raise DataCorruptionError("committed guidance receipt does not match batch")

    # LLM: GuidanceAcknowledgements：已确认批次是权威；逐回执锁内写消费状态并幂等补消息，已消费也须修复漏写的历史。
    # 函数用途: 把一份已校验的模型确认批次补写到各条 guidance 回执。
    def _apply_locked(self, batch: dict[str, Any]) -> None:
        self._validate_locked(batch)
        for item in batch.get("items", []):
            key = str(item.get("dedupe_key") or "").strip()
            if not key:
                continue
            receipt_path = self.storage.guidance_dedupe_path(key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            with locked_file_transition(transition):
                receipt = self.ledger.read_receipt(receipt_path)
                if receipt.status != "consumed":
                    receipt = replace(receipt, status="consumed", updated_at=time.time())
                    write_json_file_atomic(receipt_path, receipt.to_dict())
                self.project_transcript(receipt.entry)

    # LLM: GuidanceAcknowledgements：只由已消费回执投影消息；沿 guidance ID 幂等追加原 transcript，失败返回 False 保留恢复路径。
    # 函数用途: 把已消费补充消息按 guidance ID 最多写入一次对应会话记录。
    def project_transcript(self, entry: GuidanceEntry) -> bool:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        if metadata.get("record_in_transcript") is not True:
            return True
        guidance_id = str(entry.guidance_id or "").strip()
        thread_id = str(metadata.get("thread_id") or "").strip()
        message = str(entry.message or "").strip()
        if not guidance_id or not thread_id or not message:
            return False
        attribution: dict[str, str] = {}
        if entry.target_type == "task" and entry.target_id:
            attribution["task_id"] = entry.target_id
        elif entry.target_type == "request" and entry.target_id:
            attribution["gateway_request_id"] = entry.target_id
        elif entry.target_type == "agent_run" and entry.target_id:
            attribution["agent_run_id"] = entry.target_id
        try:
            self._append_message_once(
                {
                    "thread_id": thread_id,
                    "role": "user",
                    "content": message,
                    "channel": str(metadata.get("channel") or "internal"),
                    "channel_message_id": str(metadata.get("channel_message_id") or ""),
                    "metadata": {
                        "kind": "active_turn_user_input",
                        "guidance_id": guidance_id,
                        **attribution,
                    },
                },
                dedupe_key=f"active-turn-input:{guidance_id}",
            )
        except Exception:
            return False
        return True

    # LLM: GuidanceAcknowledgements：终态结算持回合锁调用；先修已提交确认批次，再允许拒绝 pending，避免崩溃后错误退回状态。
    # 函数用途: 修复精确回合中已提交但尚未逐条落完的模型确认批次，返回损坏批次数。
    def repair_locked(self, turn_id: str) -> int:
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        errors = 0
        for path in sorted((self.storage.guidance_ack_batches_dir / turn_digest).glob("*.json")):
            try:
                report = read_json_file_report(path, context="conversation.guidance_ack_batch.read")
                payload = report.payload
                if (
                    report.load_error is not None
                    or payload.get("schema_version")
                    not in {
                        "conversation_guidance_ack_batch.v2",
                        "conversation_guidance_ack_batch.v3",
                    }
                    or str(payload.get("expected_turn_id") or "") != turn_id
                ):
                    raise DataCorruptionError("conversation guidance ack batch is invalid")
                self._apply_locked(payload)
                self.ledger.mark_delivered(
                    tuple(
                        str(item.get("guidance_id") or "")
                        for item in payload.get("items", [])
                        if isinstance(item, dict)
                    ),
                    now=float(payload.get("committed_at") or time.time()),
                )
            except Exception:
                errors += 1
        return errors
