# LLM: 提供插话入队、认领和读取；修改须核对 Gateway、主子运行循环与精确回合隔离测试。
# 模块用途: 提供插话入队、认领和读取。
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from ..io.jsonl import append_jsonl
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .models import GuidanceEntry, MessageLogEntry, normalize_guidance_target_type
from .session_messaging import SESSION_MESSAGE_ORIGIN_KIND, SESSION_TASK_ORIGIN_KIND
from .store_guidance_acknowledgements import GuidanceAcknowledgements
from .store_guidance_ledger import GuidanceLedger
from .store_guidance_records import (
    GUIDANCE_INPUT_DIGEST_VERSION,
    GuidanceOnceReceipt,
    _guidance_entries,
    _guidance_entry_from_request,
    _guidance_input_digest,
    guidance_receipt_input_matches,
)
from .store_guidance_recovery import GuidanceRecovery
from .store_guidance_submission import GuidanceSubmissions
from .store_io import read_jsonl_report
from .store_layout import ConversationStorage


# LLM: 会话消息与派活正文由没有 subscribe/ack 的发送方写入，写时无法知道接收回合；只有目标回合
#   真正认领这条消息时才知道该绑到哪个精确回合。缺失该标记的回执在提交校验里必然不匹配，
#   会把接收方自己的请求打断（真实链路上测出的"目标正忙时发消息就让目标请求失败"）。
# 函数用途: 判断一条回执是否由另一会话的宿主投递写入，需要在认领时补记目标回合。
def _host_delivery_receipt(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    origin_kind = str(metadata.get("origin_kind") or "").strip()
    if origin_kind not in {SESSION_MESSAGE_ORIGIN_KIND, SESSION_TASK_ORIGIN_KIND}:
        return False
    return str(metadata.get("origin_thread_id") or "").strip() != ""


# LLM: 只读回执级 migration.released_turn_ids（回合收尾释放时写入）；claim_for_turn 与 available_for_turn 共用，
#   保证"能不能认领"和"算不算待处理输入"是同一条判据。
# 函数用途: 判断一条绑定在别的回合上的回执，是否已被那一回合释放、允许跨回合认领。
def _released_from(receipt: GuidanceOnceReceipt, receipt_turn_id: str) -> bool:
    released = receipt.migration.get("released_turn_ids")
    released_ids = released if isinstance(released, list) else []
    return receipt_turn_id in {str(item or "").strip() for item in released_ids}


# LLM: 认领时补记/改绑接收回合的两种情形：宿主投递回执还没绑定回合（首次认领）；会话消息被上一回合释放后由别的回合
#   跨回合认领（调用方已确认它在 released_turn_ids 里）。插话和派活正文的跨回合认领不改绑，行为不变。
# 函数用途: 判断这次认领是否要把回执的 expected_turn_id 写成当前回合并补回合索引。
def _binds_on_claim(metadata: dict, receipt_turn_id: str, requested_turn_id: str) -> bool:
    if not _host_delivery_receipt(metadata):
        return False
    if not receipt_turn_id:
        return True
    origin_kind = str(metadata.get("origin_kind") or "").strip()
    return origin_kind == SESSION_MESSAGE_ORIGIN_KIND and receipt_turn_id != requested_turn_id


# LLM: 派活正文的幂等键形状是 `body:session_task:<发送方>-><目标>:<正文摘要>`，里面带着它属于
#   哪条任务吗——不，任务编号由 create_session_task 在创建记录时写进别处。正文条目本身只带
#   origin_kind/origin_thread_id，所以"哪一轮该认领它"只能由**会话任务记录**回答，不能从键里猜。
# 函数用途: 判断一条投递正文是否允许被指定回合认领（当前只对派活正文做归属核对）。
def _body_belongs_to_task(metadata: dict[str, Any], owning_task_id: str) -> bool:
    task_id = str(metadata.get("session_task_id") or "").strip()
    if not task_id:
        # 正文写入时没记任务编号（旧数据/普通消息）：保持原行为，不新增拒绝。
        return True
    return task_id == owning_task_id


# LLM: 提供插话入队、认领和读取；修改须核对 Gateway、主子运行循环与精确回合隔离测试。
# 类用途: 提供插话入队、认领和读取。
class GuidanceStore:
    # LLM: 所有组件共用原 storage；仅确认组件获得消息幂等追加能力，不新增持久目录或转发旧 API。
    # 函数用途: 组装插话接收、提交、确认和恢复能力，构造期间不读写消息。
    def __init__(
        self, storage: ConversationStorage, *,
        append_message_once: Callable[..., MessageLogEntry],
    ) -> None:
        self.storage = storage
        self.ledger = GuidanceLedger(storage)
        self.submissions = GuidanceSubmissions(self.ledger)
        self.acknowledgements = GuidanceAcknowledgements(
            self.ledger, submissions=self.submissions, append_message_once=append_message_once,
        )
        self.recovery = GuidanceRecovery(
            self.ledger, submissions=self.submissions,
            acknowledgements=self.acknowledgements, recent_report=self.recent_report,
        )

    # LLM: GuidanceStore：内部无幂等入队会追加 JSONL；有稳定入口身份的外部消息必须调用 append_once，联测 Gateway 入口。
    # 函数用途: 追加一条不带重试语义的内部补充消息。
    def append(self, request: dict[str, Any]) -> GuidanceEntry:
        entry = _guidance_entry_from_request(request)
        append_jsonl(
            self.storage.guidance_path(entry.target_type, entry.target_id),
            entry.to_dict(),
            sort_keys=True,
        )
        return entry

    # LLM: GuidanceStore：回执锁内先写权威回执再修队列及索引；同键异文报错，重试必须返回原消息身份。
    # 函数用途: 按稳定消息 ID 只追加一次补充消息，进程崩溃后重试也会返回同一条记录。
    def append_once(
        self,
        request: dict[str, Any],
        *,
        dedupe_key: str,
    ) -> GuidanceEntry:
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("guidance dedupe_key is required")
        digest = _guidance_input_digest(request)
        receipt_path = self.storage.guidance_dedupe_path(key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self.ledger.read_receipt(receipt_path)
            if receipt is not None:
                # 旧回执按写入时的口径比对（见 guidance_receipt_input_matches），同键重试必须回到同一条。
                if receipt.dedupe_key != key or not guidance_receipt_input_matches(receipt, request):
                    raise DataCorruptionError(
                        f"conversation guidance dedupe key reused with different input: {key}"
                    )
                if receipt.status in {"pending", "reserved", "submitted"}:
                    self.ledger.repair_projections(receipt)
                return receipt.entry
            metadata = request.get("metadata")
            prepared_request = {
                **request,
                "metadata": {
                    **(metadata if isinstance(metadata, dict) else {}),
                    "dedupe_key": key,
                },
            }
            entry = _guidance_entry_from_request(prepared_request)
            receipt = GuidanceOnceReceipt(
                dedupe_key=key,
                input_digest=digest,
                status="pending",
                entry=entry,
                updated_at=time.time(),
                input_digest_version=GUIDANCE_INPUT_DIGEST_VERSION,
            )
            write_json_file_atomic(receipt_path, receipt.to_dict())
            self.ledger.repair_projections(receipt)
            return entry

    # LLM: GuidanceStore：回执锁内读取并修投影；缺失不代表已消费，坏账显式失败，Gateway 超时对账使用此入口。
    # 函数用途: 查询一条补充消息的持久回执，供 Gateway 对账网络超时。
    def receipt(self, dedupe_key: str) -> GuidanceOnceReceipt | None:
        key = str(dedupe_key or "").strip()
        if not key:
            return None
        receipt_path = self.storage.guidance_dedupe_path(key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self.ledger.read_receipt(receipt_path)
            if receipt is None:
                return None
            if receipt.dedupe_key != key:
                raise DataCorruptionError("conversation guidance receipt key mismatch")
            if receipt.status in {"pending", "reserved", "submitted"}:
                self.ledger.repair_projections(receipt)
            delivered = self.ledger.read_delivered().get(receipt.entry.guidance_id, 0.0)
            return replace(receipt, entry=replace(receipt.entry, delivered_at=delivered))

    # LLM: GuidanceStore：竞争状态沿同一回执锁决出胜者；只允许原有迁移边，不覆盖已消费或拒绝，联测 Gateway 终态。
    # 函数用途: 原子推进补充消息状态，并把并发竞争中真正获胜的状态返回给调用方。
    def mark_status(self, dedupe_key: str, status: str) -> GuidanceOnceReceipt:
        key = str(dedupe_key or "").strip()
        normalized = str(status or "").strip().lower()
        if not key or normalized not in {"submitted", "consumed", "rejected"}:
            raise ValueError("guidance receipt requires submitted, consumed, or rejected status")
        receipt_path = self.storage.guidance_dedupe_path(key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self.ledger.read_receipt(receipt_path)
            if receipt is None:
                raise KeyError(f"conversation guidance receipt not found: {key}")
            if receipt.dedupe_key != key:
                raise DataCorruptionError("conversation guidance receipt key mismatch")
            if receipt.status == normalized:
                return receipt
            allowed = (
                receipt.status == "pending" and normalized == "rejected"
            ) or (
                receipt.status == "reserved" and normalized == "submitted"
            ) or (
                receipt.status == "submitted" and normalized == "consumed"
            )
            if not allowed:
                return receipt
            updated = replace(receipt, status=normalized, updated_at=time.time())
            write_json_file_atomic(receipt_path, updated.to_dict())
            return updated

    # LLM: GuidanceStore：安全点仅为精确尝试预留 pending；跨回合例外必须由持久释放记录授权，联测主子邮箱隔离。
    # 函数用途: 在模型安全点为当前执行尝试首次预留补充消息，其他尝试不能重复注入。
    def claim_for_turn(
        self,
        entry: GuidanceEntry,
        *,
        expected_turn_id: str,
        attempt_id: str,
        owning_task_id: str = "",
    ) -> bool:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return True
        receipt_path = self.storage.guidance_dedupe_path(dedupe_key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self.ledger.read_receipt(receipt_path)
            if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
                raise DataCorruptionError("conversation guidance receipt entry mismatch")
            receipt_metadata = (
                receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
            )
            receipt_turn_id = str(receipt_metadata.get("expected_turn_id") or "").strip()
            requested_turn_id = str(expected_turn_id or "").strip()
            if receipt_turn_id and receipt_turn_id != requested_turn_id:
                # 例外只有一种：那一轮在认领前就结束了，回合终态已把回执标记为"释放"。
                # 未被释放的回执仍然拒绝跨轮认领（保持"插话只属于当时的活动轮次"）。
                if not _released_from(receipt, receipt_turn_id):
                    return False
            if receipt.status != "pending":
                return False
            # 派活正文在写入时就带着"这条正文属于哪条任务"（幂等键里有任务编号），而派活回合自带
            # 同一个编号。两者不一致时这一轮不许认领：否则先到的回合会把别人（往往是更早那条）的
            # 正文抢走做完，就是真实链路上"B 做掉了更早的 B1"的成因。
            if owning_task_id and not _body_belongs_to_task(receipt_metadata, owning_task_id):
                return False
            normalized_attempt_id = str(attempt_id or "").strip()
            if not normalized_attempt_id:
                raise ValueError("guidance reservation requires attempt_id")
            updated = replace(
                receipt,
                status="reserved",
                attempt_id=normalized_attempt_id,
                updated_at=time.time(),
            )
            if _binds_on_claim(receipt_metadata, receipt_turn_id, requested_turn_id):
                # 宿主投递（另一会话的消息或派活正文）写时没有接收回合；在认领这一刻补记，
                # 与交互式插话共用同一条预约语义，提交校验才能对得上这条回执。
                # 被上一回合释放的会话消息跨回合认领时同样改绑到这一回合：这一回合再没消费就结束时，
                # 回合收尾才能按它的索引找到它、再记一次释放（释放上限据此计数）。
                bound_metadata = dict(receipt_metadata)
                bound_metadata["expected_turn_id"] = requested_turn_id
                updated = replace(
                    updated,
                    entry=replace(receipt.entry, metadata=bound_metadata),
                )
                # 精确回合索引是"这一轮有哪些补充消息"的投影；绑定时一起补，回合收尾才能按它找到。
                self.ledger.ensure_turn_index(updated)
            write_json_file_atomic(receipt_path, updated.to_dict())
            return True

    # LLM: "有没有可认领的新输入"必须和"能不能认领"用同一个判定。两者不一致时（真实链路上
    #   `claim_for_turn` 按 owning_task_id 拒绝，而这里不看归属照样说"有待处理"），运行循环会
    #   每次都判定"有未处理输入"、把已完成的回复作废重来，形成无限空转（3.2 秒 88 次模型调用）。
    #   所以这里逐条走与认领完全相同的准入判据，只把写操作去掉（无副作用）。
    # 函数用途: 判断一条补充消息能否由指定回合认领，供运行循环做无副作用的待处理检查。
    def available_for_turn(
        self,
        entry: GuidanceEntry,
        *,
        expected_turn_id: str,
        owning_task_id: str = "",
    ) -> bool:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return True
        receipt = self.receipt(dedupe_key)
        if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
            raise DataCorruptionError("conversation guidance receipt entry mismatch")
        receipt_metadata = (
            receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        )
        receipt_turn_id = str(receipt_metadata.get("expected_turn_id") or "").strip()
        if receipt_turn_id and receipt_turn_id != str(expected_turn_id or "").strip() and not _released_from(
            receipt, receipt_turn_id,
        ):
            return False
        # 与 claim_for_turn 完全同一条归属判据：不属于本回合的投递既不能认领，也不算待处理输入。
        if owning_task_id and not _body_belongs_to_task(receipt_metadata, owning_task_id):
            return False
        return receipt.status == "pending"

    # LLM: GuidanceStore：按 owner 内精确输入索引查回执；反查冲突显式失败，不扫描历史正文猜归属。
    # 函数用途: 按 Gateway 普通消息请求 ID 查询已建立的 guidance 回执和精确回合。
    def receipt_for_input(
        self,
        gateway_input_request_id: str,
    ) -> tuple[GuidanceOnceReceipt | None, str]:
        input_request_id = str(gateway_input_request_id or "").strip()
        if not input_request_id:
            return None, ""
        index_path = self.storage.guidance_input_index_path(input_request_id)
        report = read_json_file_report(index_path, context="conversation.guidance_input_index.read")
        if report.load_error is not None:
            raise DataCorruptionError("conversation guidance input index is unreadable")
        if not report.payload:
            return None, ""
        if (
            report.payload.get("schema_version") != "conversation_guidance_input_ref.v1"
            or str(report.payload.get("gateway_input_request_id") or "") != input_request_id
        ):
            raise DataCorruptionError("conversation guidance input index is invalid")
        dedupe_key = str(report.payload.get("dedupe_key") or "").strip()
        if not dedupe_key:
            raise DataCorruptionError("conversation guidance input index has no receipt key")
        receipt = self.receipt(dedupe_key)
        metadata = (
            receipt.entry.metadata
            if receipt is not None and isinstance(receipt.entry.metadata, dict)
            else {}
        )
        return receipt, str(metadata.get("expected_turn_id") or "").strip()

    # LLM: GuidanceStore：只提供列表便捷读取；需要辨别坏账的调用方使用 recent_report，不据空列表推断交付成功。
    # 函数用途: 读取目标最近的消息；需要坏账详情的调用方使用 recent_report。
    def recent(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> list[GuidanceEntry]:
        entries, _errors = self.recent_report(
            target_type,
            target_id,
            limit=limit,
            include_delivered=include_delivered,
        )
        return entries

    # LLM: GuidanceStore：从未创建的队列为空；现有坏账保留错误，pending 必须使用权威回执投影，联测恢复改绑。
    # 函数用途: 读取指定对象的补充消息；从未收到过补充时返回空列表，不把未创建队列误报成损坏。
    def recent_report(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
        normalized_type = normalize_guidance_target_type(target_type)
        if not normalized_type:
            return [], [
                {"code": "GUIDANCE_TARGET_TYPE_INVALID", "target_type": str(target_type or "")}
            ]
        delivered = self.ledger.read_delivered()
        path = self.storage.guidance_path(normalized_type, str(target_id))
        if not path.exists():
            return [], []
        report = read_jsonl_report(
            path,
            context="conversation.guidance.read",
        )
        entries, parse_errors = _guidance_entries(report.rows, delivered)
        if not include_delivered:
            pending_entries: list[GuidanceEntry] = []
            for item in entries:
                try:
                    pending = self._canonical_pending(item)
                    if pending is not None:
                        pending_entries.append(pending)
                except Exception as exc:
                    error = runtime_error_report(exc, context="conversation.guidance.receipt")
                    error["guidance_id"] = item.guidance_id
                    parse_errors.append(error)
            entries = pending_entries
        selected = entries if limit <= 0 else entries[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    # LLM: GuidanceStore：幂等输入按回执决定可注入内容，未知提交不得重放；内部无幂等输入沿原送达索引。
    # 函数用途: 返回仍可注入的权威 guidance；已消费、已拒绝和提交结果未知的记录不会再次出现。
    def _canonical_pending(
        self,
        entry: GuidanceEntry,
    ) -> GuidanceEntry | None:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return entry if entry.delivered_at <= 0 else None
        receipt = self.receipt(dedupe_key)
        if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
            raise DataCorruptionError("conversation guidance receipt entry mismatch")
        if receipt.status != "pending":
            return None
        return replace(receipt.entry, delivered_at=entry.delivered_at)

    # LLM: GuidanceStore：通过同一读取路径取可注入消息；回执校验及投影修复可能写盘，不能代替正式认领。
    # 函数用途: 读取仍可注入的消息，回执校验与修复沿统一读取路径执行。
    def pending(
        self, target_type: str, target_id: str, *, limit: int = 20
    ) -> list[GuidanceEntry]:
        return self.recent(target_type, target_id, limit=limit, include_delivered=False)

    # LLM: GuidanceStore：返回可注入消息及加载错误；运行循环、恢复与上下文须保留错误，不能吞成成功空队列。
    # 函数用途: 读取可注入消息及其错误，不把损坏回执误当空队列。
    def pending_report(
        self, target_type: str, target_id: str, *, limit: int = 20
    ) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
        return self.recent_report(
            target_type, target_id, limit=limit, include_delivered=False
        )
