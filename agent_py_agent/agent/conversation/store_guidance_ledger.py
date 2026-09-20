# LLM: 维护回执与队列、索引投影；读取可能执行显式迁移，修改须核对幂等及崩溃恢复测试。
# 模块用途: 维护回执与队列、索引投影。
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file,
    read_json_file_report,
    update_json_file_atomic,
    write_json_file_atomic,
)
from ..io.jsonl import append_jsonl
from ..runtime_errors import DataCorruptionError
from .models import GuidanceEntry
from .store_guidance_records import (
    GuidanceOnceReceipt,
    _guidance_queue_entry_matches_receipt,
    _guidance_rebound_from_turn_ids,
    _legacy_prompt_submission_unknown_receipt,
)
from .store_io import read_jsonl_report, unlink_quietly
from .store_layout import ConversationStorage


# LLM: 维护回执与队列、索引投影；读取可能执行显式迁移，修改须核对幂等及崩溃恢复测试。
# 类用途: 维护回执与队列、索引投影。
class GuidanceLedger:
    # LLM: 只绑定唯一存储上下文；初始化不读写回执、迁移数据或创建新锁。
    # 函数用途: 为插话事务提供同源路径、回执读取及可修复投影。
    def __init__(self, storage: ConversationStorage) -> None:
        self.storage = storage

    # LLM: GuidanceLedger：运行循环与终态结算共用精确回合锁；不得改变与 Gateway 外锁、回执锁的获取顺序。
    # 函数用途: 返回一个精确活动回合的补充消息状态转换锁。
    def turn_guard(self, expected_turn_id: str):
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            raise ValueError("guidance turn transition requires expected_turn_id")
        digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        return locked_file_transition(
            self.storage.guidance_turn_index_dir / f".{digest}.transition"
        )

    # LLM: GuidanceLedger：调用方已持有回执锁；坏账与缺失分开，旧版迁移会写回，不得把旧送达标记当成模型确认。
    # 函数用途: 在已持有 transition lock 时读取校验回执，并封存旧版已送达但未结算的模糊状态。
    def read_receipt(self, path: Path) -> GuidanceOnceReceipt | None:
        report = read_json_file_report(path, context="conversation.guidance_once.read")
        if report.load_error is not None:
            raise DataCorruptionError(f"conversation guidance receipt is unreadable: {path.name}")
        if not report.payload:
            if path.exists():
                raise DataCorruptionError(
                    f"conversation guidance receipt is empty or invalid: {path.name}"
                )
            return None
        receipt = GuidanceOnceReceipt.from_dict(report.payload)
        if report.payload.get("schema_version") != "conversation_guidance_once.v4":
            write_json_file_atomic(path, receipt.to_dict())
        delivered_at = float(
            self.read_delivered().get(receipt.entry.guidance_id) or 0.0
        )
        if delivered_at > 0 and receipt.status in {"pending", "reserved"}:
            metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
            receipt = _legacy_prompt_submission_unknown_receipt(
                receipt,
                old_turn_id=str(metadata.get("expected_turn_id") or "").strip(),
                delivered_at=delivered_at,
            )
            write_json_file_atomic(path, receipt.to_dict())
        return receipt

    # LLM: GuidanceLedger：回执是权威；先修新回合、输入引用及队列，最后清旧引用，保证半写入后仍可发现。
    # 函数用途: 一次性修复补充消息的队列和索引投影，收口恢复改绑过程中的半写入状态。
    def repair_projections(self, receipt: GuidanceOnceReceipt) -> None:
        self.ensure_turn_index(receipt)
        self._ensure_input_index(receipt)
        self.ensure_queue_entry(receipt.entry)
        self._retire_rebound_indexes(receipt)

    # LLM: GuidanceLedger：索引只定位回执；精确回合冲突必须报错，不能覆盖其它回执的引用。
    # 函数用途: 修复精确回合的索引引用，冲突时保留原文件并报错。
    def ensure_turn_index(self, receipt: GuidanceOnceReceipt) -> None:
        metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        turn_id = str(metadata.get("expected_turn_id") or "").strip()
        if not turn_id:
            return
        index_path = self.storage.guidance_turn_index_path(turn_id, receipt.dedupe_key)
        expected = {
            "schema_version": "conversation_guidance_turn_ref.v1",
            "expected_turn_id": turn_id,
            "dedupe_key": receipt.dedupe_key,
        }
        report = read_json_file_report(index_path, context="conversation.guidance_turn_index.read")
        if report.load_error is not None:
            raise DataCorruptionError("conversation guidance turn index is unreadable")
        if report.payload:
            if report.payload != expected:
                raise DataCorruptionError("conversation guidance turn index conflicts")
            return
        write_json_file_atomic(index_path, expected)

    # LLM: GuidanceLedger：Gateway 输入反查只接受同一幂等身份；仅回执内明确改绑链可授权修复旧回合引用。
    # 函数用途: 为 Gateway 普通消息写反向索引，避免 guidance 已写但入口回执仍 pending 时失联。
    def _ensure_input_index(self, receipt: GuidanceOnceReceipt) -> None:
        metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        input_request_id = str(metadata.get("gateway_input_request_id") or "").strip()
        if not input_request_id:
            return
        index_path = self.storage.guidance_input_index_path(input_request_id)
        expected = {
            "schema_version": "conversation_guidance_input_ref.v1",
            "gateway_input_request_id": input_request_id,
            "dedupe_key": receipt.dedupe_key,
            "expected_turn_id": str(metadata.get("expected_turn_id") or "").strip(),
        }
        report = read_json_file_report(index_path, context="conversation.guidance_input_index.read")
        if report.load_error is not None:
            raise DataCorruptionError("conversation guidance input index is unreadable")
        if report.payload:
            if report.payload == expected:
                return
            existing_turn = str(report.payload.get("expected_turn_id") or "").strip()
            rebound_from = _guidance_rebound_from_turn_ids(receipt)
            same_identity = (
                report.payload.get("schema_version")
                == "conversation_guidance_input_ref.v1"
                and str(report.payload.get("gateway_input_request_id") or "").strip()
                == input_request_id
                and str(report.payload.get("dedupe_key") or "").strip()
                == receipt.dedupe_key
            )
            if not same_identity or existing_turn not in rebound_from:
                raise DataCorruptionError("conversation guidance input index conflicts")
        write_json_file_atomic(index_path, expected)

    # LLM: GuidanceLedger：调用方已修好新投影；只按回执改绑链删除旧引用，不扫描文字或猜测旧尝试。
    # 函数用途: 清除恢复改绑留下的旧 attempt 索引，避免旧回合结算时误报损坏。
    def _retire_rebound_indexes(self, receipt: GuidanceOnceReceipt) -> None:
        metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        current_turn = str(metadata.get("expected_turn_id") or "").strip()
        for old_turn in _guidance_rebound_from_turn_ids(receipt):
            if old_turn and old_turn != current_turn:
                unlink_quietly(
                    self.storage.guidance_turn_index_path(old_turn, receipt.dedupe_key)
                )

    # LLM: GuidanceLedger：队列行不可变；相同编号内容冲突报错，恢复改绑仅容许宿主回合字段变化，联测崩溃重放。
    # 函数用途: 确保回执对应的 guidance 队列行存在且只存在一次。
    def ensure_queue_entry(self, entry: GuidanceEntry) -> None:
        path = self.storage.guidance_path(entry.target_type, entry.target_id)
        if not path.exists():
            append_jsonl(path, entry.to_dict(), sort_keys=True)
            return
        report = read_jsonl_report(path, context="conversation.guidance_once.queue")
        if report.load_errors:
            raise DataCorruptionError(
                f"conversation guidance queue is unreadable: {entry.target_type}:{entry.target_id}"
            )
        matches = [
            GuidanceEntry.from_dict(row)
            for row in report.rows
            if str(row.get("guidance_id") or "") == entry.guidance_id
        ]
        if matches:
            if len(matches) != 1 or not _guidance_queue_entry_matches_receipt(
                matches[0], entry
            ):
                raise DataCorruptionError(
                    f"conversation guidance id has conflicting rows: {entry.guidance_id}"
                )
            return
        append_jsonl(path, entry.to_dict(), sort_keys=True)

    # LLM: GuidanceLedger：原子更新原送达索引；这是内部无幂等消息的退休事实，不能替代幂等回执的消费确认。
    # 函数用途: 原子更新非幂等内部消息的送达投影，并供确认批次修复使用。
    def mark_delivered(
        self, guidance_ids: list[str] | tuple[str, ...], *, now: float | None = None
    ) -> None:
        ids = [str(item).strip() for item in guidance_ids if str(item or "").strip()]
        if not ids:
            return
        delivered_at = now if now is not None else time.time()
        update_json_file_atomic(
            self.storage.guidance_delivered_path,
            lambda data: {**data, **dict.fromkeys(ids, delivered_at)},
        )

    # LLM: GuidanceLedger：送达索引是投影；保留旧无效数值的忽略语义，不在读取时推进消费状态。
    # 函数用途: 读取已有送达索引，忽略不可解析的旧数值，不推进回执状态。
    def read_delivered(self) -> dict[str, float]:
        delivered: dict[str, float] = {}
        for key, value in read_json_file(self.storage.guidance_delivered_path).items():
            try:
                delivered[str(key)] = float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return delivered

    # LLM: GuidanceLedger：改绑只覆盖同键、同旧回合的输入反查；修改须联测恢复中断后的索引修复。
    # 函数用途: 同步更新普通消息回执的反向索引，保证恢复后查询指向新的子代理 attempt。
    def rebind_input_index(
        self,
        receipt: GuidanceOnceReceipt,
        *,
        old_turn_id: str,
    ) -> None:
        metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        input_request_id = str(metadata.get("gateway_input_request_id") or "").strip()
        if not input_request_id:
            return
        path = self.storage.guidance_input_index_path(input_request_id)
        report = read_json_file_report(path, context="conversation.guidance_input_index.rebind")
        if report.load_error is not None:
            raise DataCorruptionError("conversation guidance input index is unreadable")
        payload = report.payload
        if payload and (
            str(payload.get("dedupe_key") or "") != receipt.dedupe_key
            or str(payload.get("expected_turn_id") or "") != old_turn_id
        ):
            raise DataCorruptionError("conversation guidance input index conflicts")
        write_json_file_atomic(
            path,
            {
                "schema_version": "conversation_guidance_input_ref.v1",
                "gateway_input_request_id": input_request_id,
                "dedupe_key": receipt.dedupe_key,
                "expected_turn_id": str(metadata.get("expected_turn_id") or ""),
            },
        )
