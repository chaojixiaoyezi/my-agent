from __future__ import annotations

"""LLM: Gateway 负责执行请求，本模块只持久化回送路由并轮询既有 request_id，禁止重提任务。

模块用途: 让飞书等交互通道的回调立即返回；长任务完成后由可恢复后台线程把真实结果送回用户。
"""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..common.json_io import locked_json_path

_LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = 6
_SENT_RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_CLAIM_TTL_SECONDS = 120.0
_PENDING_WATCH_KINDS = frozenset(
    {"input_receipt", "request_result", "control_receipt"}
)
_CONTROL_RECEIPT_STATES = frozenset(
    {"prepared", "executing", "completed", "terminal_unknown"}
)


# LLM: Auth/config failures are typed transport quarantine signals, never user-visible reply text.
# 类用途: 通知回送 worker 持久隔离无法继续对账的 Gateway watcher。
class GatewayReplyQuarantineError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "gateway_reply_quarantine")
        super().__init__(self.reason)


# LLM: Control polling returns structured operation and delivery state. Display text can be sent
# only after these fields establish a completed accepted/rejected outcome.
# 类用途: 表示一次 `/control-status` 查询的结构化结果，避免从提示文案推断控制操作是否送达。
@dataclass(frozen=True)
class GatewayControlReceiptResult:
    control_state: str
    delivery_status: str = ""
    message: str = ""
    target_turn_id: str = ""
    control_kind: str = ""

    # LLM: The closed state set mirrors the Gateway operation receipt schema; transport WAIT is
    # represented by a None poll result rather than an invented state string.
    # 函数用途: 校验控制操作阶段和投递三态，拒绝损坏的 Gateway 返回。
    def __post_init__(self) -> None:
        control_state = str(self.control_state or "").strip().lower()
        delivery_status = str(self.delivery_status or "").strip().lower()
        if control_state not in _CONTROL_RECEIPT_STATES:
            raise ValueError(f"invalid gateway control receipt state: {control_state}")
        if delivery_status not in {"", "accepted", "rejected", "unknown"}:
            raise ValueError(
                f"invalid gateway control delivery status: {delivery_status}"
            )
        object.__setattr__(self, "control_state", control_state)
        object.__setattr__(self, "delivery_status", delivery_status)
        object.__setattr__(
            self,
            "target_turn_id",
            str(self.target_turn_id or "").strip(),
        )
        object.__setattr__(
            self,
            "control_kind",
            str(self.control_kind or "").strip().lower(),
        )


# LLM: Claim configuration is one transport concern shared by ingress and reply workers. Empty
# owner means generate a process-unique owner; TTL is normalized once at composition time.
# 类用途: 集中配置 adapter worker 的跨进程租约身份和有效期，避免构造函数散落多个租约参数。
@dataclass(frozen=True)
class GatewayClaimLeaseConfig:
    owner: str = ""
    ttl_seconds: float = _DEFAULT_CLAIM_TTL_SECONDS

    # LLM: A caller-supplied owner is opaque but non-whitespace; TTL never falls below one second.
    # 函数用途: 规范化租约配置，供两个 worker 生成一致的 claim 行为。
    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", str(self.owner or "").strip())
        object.__setattr__(
            self,
            "ttl_seconds",
            max(1.0, float(self.ttl_seconds)),
        )


# LLM: PendingGatewayReply 只保存回送所需身份，不保存 prompt、回复正文或凭据。
# 类用途: 表示一个已经提交 Gateway、等待送回原通道的回复。
@dataclass(frozen=True)
class PendingGatewayReply:
    request_id: str
    channel: str
    user_id: str
    message_id: str
    conversation_id: str = ""
    channel_chat_type: str = ""
    channel_chat_id: str = ""
    progress_handle: str = ""
    progress_cursor: int = 0
    created_at: float = 0.0
    delivery_attempts: int = 0
    next_delivery_at: float = 0.0
    watch_kind: str = "request_result"
    operation_id: str = ""
    receipt_id: str = ""
    control_kind: str = ""
    claim_owner: str = ""
    claim_epoch: int = 0
    claim_expires_at: float = 0.0

    # LLM: watch_kind is a closed transport state, not a display label; invalid values must fail
    # before a record can be persisted or polled through the wrong endpoint.
    # 函数用途: 校验待回送记录当前监听的是输入去向还是任务结果。
    def __post_init__(self) -> None:
        normalized = str(self.watch_kind or "").strip().lower()
        if normalized not in _PENDING_WATCH_KINDS:
            raise ValueError(f"invalid pending gateway reply watch_kind: {normalized}")
        claim_owner = str(self.claim_owner or "").strip()
        claim_epoch = max(0, int(self.claim_epoch or 0))
        claim_expires_at = max(0.0, float(self.claim_expires_at or 0.0))
        if claim_owner and claim_epoch <= 0:
            raise ValueError("pending gateway reply claim owner requires a positive epoch")
        if not claim_owner:
            claim_expires_at = 0.0
        operation_id = str(self.operation_id or "").strip()
        receipt_id = str(self.receipt_id or "").strip()
        control_kind = str(self.control_kind or "").strip().lower()
        channel_chat_type = str(self.channel_chat_type or "").strip().lower()
        channel_chat_id = str(self.channel_chat_id or "").strip()
        if normalized == "control_receipt":
            if not operation_id or not receipt_id or not control_kind:
                raise ValueError(
                    "control receipt watcher requires operation_id, receipt_id, and kind"
                )
            if operation_id != receipt_id:
                raise ValueError("control receipt operation_id conflicts with receipt_id")
        object.__setattr__(self, "watch_kind", normalized)
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "control_kind", control_kind)
        object.__setattr__(self, "channel_chat_type", channel_chat_type)
        object.__setattr__(self, "channel_chat_id", channel_chat_id)
        object.__setattr__(self, "claim_owner", claim_owner)
        object.__setattr__(self, "claim_epoch", claim_epoch)
        object.__setattr__(self, "claim_expires_at", claim_expires_at)

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PendingGatewayReply:
        request_id = str(data.get("request_id") or "").strip()
        channel = str(data.get("channel") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        watch_kind = str(data.get("watch_kind") or "request_result").strip().lower()
        operation_id = str(data.get("operation_id") or "").strip()
        if not (channel and user_id) or (
            watch_kind != "control_receipt" and not request_id
        ) or (watch_kind == "control_receipt" and not operation_id):
            raise ValueError("pending gateway reply is missing request/channel/user identity")
        return cls(
            request_id=request_id,
            channel=channel,
            user_id=user_id,
            message_id=str(data.get("message_id") or ""),
            conversation_id=str(data.get("conversation_id") or ""),
            channel_chat_type=str(data.get("channel_chat_type") or ""),
            channel_chat_id=str(data.get("channel_chat_id") or ""),
            progress_handle=str(data.get("progress_handle") or ""),
            watch_kind=watch_kind,
            operation_id=operation_id,
            receipt_id=str(data.get("receipt_id") or "").strip(),
            control_kind=str(data.get("control_kind") or "").strip().lower(),
            progress_cursor=max(0, int(data.get("progress_cursor") or 0)),
            created_at=float(data.get("created_at") or 0.0),
            delivery_attempts=max(0, int(data.get("delivery_attempts") or 0)),
            next_delivery_at=max(0.0, float(data.get("next_delivery_at") or 0.0)),
            claim_owner=str(data.get("claim_owner") or "").strip(),
            claim_epoch=max(0, int(data.get("claim_epoch") or 0)),
            claim_expires_at=max(0.0, float(data.get("claim_expires_at") or 0.0)),
        )

    # LLM: Control operations are keyed by their independent operation receipt, never by the target
    # turn request id. Ordinary input/result watchers retain request_id as their stable key.
    # 函数用途: 返回 pending 文件、claim 和终态回执共用的唯一稳定身份。
    @property
    def stable_id(self) -> str:
        return self.operation_id if self.watch_kind == "control_receipt" else self.request_id


# LLM: Input/control receipt polling and placeholder cleanup form one post-submit reconciliation
# capability; bundling them keeps worker construction small without creating a second execution path.
# 类用途: 组合输入回执、控制回执和占位清理回调，未配置的能力保持显式 None。
@dataclass(frozen=True)
class GatewayReplyReceiptCallbacks:
    poll_input: Callable[[PendingGatewayReply], str] | None = None
    poll_control: Callable[
        [PendingGatewayReply], GatewayControlReceiptResult | None
    ] | None = None
    clear_placeholder: Callable[[PendingGatewayReply], None] | None = None


# LLM: This mixin contains only per-record lease transitions. The concrete store supplies paths,
# locks, and receipt lookups; no network or provider callback belongs in these methods.
# 类用途: 把 claim/commit/release/退避集中成独立的跨进程 fencing 能力，缩短主存储类但不新增状态源。
class _GatewayReplyClaimStoreMixin:
    # LLM: Cross-process workers acquire one short record-file claim before polling or provider IO.
    # Expired claims are fenced by a strictly increasing epoch.
    # 函数用途: 领取待回送记录的 owner/epoch/expiry 租约，不在锁内执行网络或通道调用。
    def claim(
        self,
        observed: PendingGatewayReply,
        *,
        owner: str,
        now: float,
        ttl: float,
    ) -> PendingGatewayReply | None:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise ValueError("pending gateway reply claim owner is required")
        expires_at = float(now) + max(1.0, float(ttl))
        stable_id = observed.stable_id
        with self._lock:
            if self.was_sent(stable_id):
                return None
            if self.root is None:
                current = self._volatile_pending.get(stable_id)
                if current != observed:
                    return None
                if current.claim_owner and current.claim_expires_at > now:
                    return None
                claimed = replace(
                    current,
                    claim_owner=normalized_owner,
                    claim_epoch=current.claim_epoch + 1,
                    claim_expires_at=expires_at,
                )
                self._volatile_pending[stable_id] = claimed
                return claimed
            self._ensure_dirs()
            path = self._pending_path(stable_id)
            with locked_json_path(path):
                if self.was_sent(stable_id):
                    return None
                current = self._read_pending(path) if path.exists() else None
                if current != observed or current is None:
                    return None
                if current.claim_owner and current.claim_expires_at > now:
                    return None
                claimed = replace(
                    current,
                    claim_owner=normalized_owner,
                    claim_epoch=current.claim_epoch + 1,
                    claim_expires_at=expires_at,
                )
                _atomic_write_json(path, claimed.to_dict())
                return claimed

    # LLM: Poll/provider results are valid only for the exact durable owner/epoch snapshot that
    # produced them. A takeover makes stale commits harmless.
    # 函数用途: 以同 epoch 提交 watcher/游标/退避结果，并按需释放租约。
    def commit_claim(
        self,
        expected: PendingGatewayReply,
        replacement: PendingGatewayReply,
        *,
        owner: str,
        epoch: int,
        release: bool = True,
    ) -> PendingGatewayReply | None:
        _ensure_same_pending_route(
            expected,
            replacement,
            allow_control_target_bind=True,
        )
        if expected.claim_owner != owner or expected.claim_epoch != int(epoch):
            return None
        committed = replace(
            replacement,
            claim_owner="" if release else owner,
            claim_epoch=int(epoch),
            claim_expires_at=0.0 if release else expected.claim_expires_at,
        )
        stable_id = expected.stable_id
        with self._lock:
            if self.was_sent(stable_id):
                return None
            if self.root is None:
                current = self._volatile_pending.get(stable_id)
                if current != expected:
                    return None
                self._volatile_pending[stable_id] = committed
                return committed
            self._ensure_dirs()
            path = self._pending_path(stable_id)
            with locked_json_path(path):
                if self.was_sent(stable_id):
                    return None
                current = self._read_pending(path) if path.exists() else None
                if current != expected:
                    return None
                _atomic_write_json(path, committed.to_dict())
                return committed

    # LLM: WAIT releases only the current epoch, never a newer claimant.
    # 函数用途: 无状态变化时释放当前回送租约。
    def release_claim(
        self,
        record: PendingGatewayReply,
        *,
        owner: str,
        epoch: int,
    ) -> PendingGatewayReply | None:
        return self.commit_claim(
            record,
            record,
            owner=owner,
            epoch=epoch,
            release=True,
        )

    # LLM: Delivery backoff is fenced by the same owner/epoch as the failed provider call.
    # 函数用途: 当前租约的发送失败后写入有界退避并释放租约。
    def defer_claimed(
        self,
        record: PendingGatewayReply,
        *,
        owner: str,
        epoch: int,
    ) -> PendingGatewayReply | None:
        attempts = record.delivery_attempts + 1
        delay = min(300.0, max(1.0, 2.0 ** min(attempts - 1, 8)))
        return self.commit_claim(
            record,
            replace(
                record,
                delivery_attempts=attempts,
                next_delivery_at=time.time() + delay,
            ),
            owner=owner,
            epoch=epoch,
            release=True,
        )


# LLM: Store 用原子 JSON + 短期 sent receipt 做重启恢复；不能把 pending 当成重新执行任务的队列。
# 类用途: 保存待回送记录和已发送回执，进程重启后可继续同一个 Gateway 请求。
class GatewayReplyDeliveryStore(_GatewayReplyClaimStoreMixin):
    """原子 pending 记录和短期 sent 回执。"""

    def __init__(self, root: Path | None) -> None:
        self.root = Path(root).expanduser() if root is not None else None
        self._volatile_pending: dict[str, PendingGatewayReply] = {}
        self._volatile_sent: set[str] = set()
        self._volatile_receipts: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    @property
    def durable(self) -> bool:
        return self.root is not None

    # LLM: First writer owns the immutable channel route for one stable Gateway id. Replays may
    # observe that record, but a different owner/message route must fail before overwriting it.
    # 函数用途: 首次保存待回送记录；同路由重放返回原记录，身份冲突则拒绝。
    def put_if_absent(
        self,
        record: PendingGatewayReply,
    ) -> tuple[PendingGatewayReply | None, bool]:
        stable_id = record.stable_id
        with self._lock:
            if self.was_sent(stable_id):
                return None, False
            if self.root is None:
                current = self._volatile_pending.get(stable_id)
                if current is not None:
                    _ensure_same_pending_route(
                        current,
                        record,
                        allow_initial_watch_replay=True,
                        allow_unbound_control_replay=True,
                    )
                    return current, False
                self._volatile_pending[stable_id] = record
                return record, True
            self._ensure_dirs()
            path = self._pending_path(stable_id)
            with locked_json_path(path):
                if self.was_sent(stable_id):
                    return None, False
                current = self._read_pending(path) if path.exists() else None
                if path.exists() and current is None:
                    raise RuntimeError("pending gateway reply record is unreadable")
                if current is not None:
                    _ensure_same_pending_route(
                        current,
                        record,
                        allow_initial_watch_replay=True,
                        allow_unbound_control_replay=True,
                    )
                    return current, False
                _atomic_write_json(path, record.to_dict())
                return record, True

    # LLM: Mutable delivery state advances only from the exact record a worker observed. The
    # request id and trusted channel route remain immutable across every transition.
    # 函数用途: 以比较并交换更新 watcher、游标或退避状态，避免旧 worker 覆盖新状态。
    def compare_and_swap(
        self,
        expected: PendingGatewayReply,
        replacement: PendingGatewayReply,
    ) -> bool:
        _ensure_same_pending_route(expected, replacement)
        stable_id = expected.stable_id
        if stable_id != replacement.stable_id:
            raise ValueError("pending gateway reply CAS stable id conflict")
        with self._lock:
            if self.was_sent(stable_id):
                return False
            if self.root is None:
                current = self._volatile_pending.get(stable_id)
                if current is None:
                    return False
                if current != expected:
                    return False
                _ensure_same_pending_route(current, replacement)
                self._volatile_pending[stable_id] = replacement
                return True
            self._ensure_dirs()
            path = self._pending_path(stable_id)
            with locked_json_path(path):
                if self.was_sent(stable_id):
                    return False
                current = self._read_pending(path) if path.exists() else None
                if path.exists() and current is None:
                    raise RuntimeError("pending gateway reply record is unreadable")
                if current is None:
                    return False
                if current != expected:
                    return False
                _ensure_same_pending_route(current, replacement)
                _atomic_write_json(path, replacement.to_dict())
                return True

    def pending(self) -> list[PendingGatewayReply]:
        with self._lock:
            if self.root is None:
                return sorted(
                    self._volatile_pending.values(),
                    key=lambda item: (item.created_at, item.stable_id),
                )
            self._ensure_dirs()
            records = [
                record
                for path in sorted((self.root / "pending").glob("*.json"))
                if (record := self._read_pending(path)) is not None
            ]
            return sorted(records, key=lambda item: (item.created_at, item.stable_id))

    def _read_pending(self, path: Path) -> PendingGatewayReply | None:
        try:
            record = PendingGatewayReply.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if self.was_sent(record.stable_id):
                path.unlink(missing_ok=True)
                return None
            return record
        except Exception as exc:
            # 坏记录必须留在原处供运维修复，不能静默删除并永久丢失回复。
            _LOGGER.error("pending channel reply record unreadable path=%s error=%s", path, exc)
            return None

    def was_sent(self, stable_id: str) -> bool:
        if stable_id in self._volatile_sent:
            return True
        if self.root is None:
            return False
        return self._sent_path(stable_id).exists()

    def mark_sent(self, record: PendingGatewayReply) -> None:
        stable_id = record.stable_id
        with self._lock:
            # Guard the already-completed external side effect in this process even if the
            # receipt filesystem becomes unhealthy after the channel accepted the message.
            self._volatile_sent.add(stable_id)
            self._volatile_receipts[stable_id] = {
                "request_id": record.request_id,
                "operation_id": record.operation_id,
                "disposition": "sent",
            }
            if self.root is None:
                self._volatile_pending.pop(stable_id, None)
                return
            self._ensure_dirs()
            pending_path = self._pending_path(stable_id)
            with locked_json_path(pending_path):
                _atomic_write_json(
                    self._sent_path(stable_id),
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "request_id": record.request_id,
                        "operation_id": record.operation_id,
                        "sent_at": time.time(),
                    },
                )
                pending_path.unlink(missing_ok=True)

    # LLM: A discard receipt is terminal delivery state for one interrupted request, preventing restart replay.
    # 函数用途: 持久标记中断请求的迟到回复已丢弃，避免重启后再发给用户。
    def mark_discarded(self, record: PendingGatewayReply, *, reason: str) -> None:
        """Retire a reply whose originating run was explicitly interrupted."""
        stable_id = record.stable_id
        with self._lock:
            self._volatile_sent.add(stable_id)
            self._volatile_receipts[stable_id] = {
                "request_id": record.request_id,
                "operation_id": record.operation_id,
                "disposition": "discarded",
                "reason": str(reason or "interrupted"),
            }
            if self.root is None:
                self._volatile_pending.pop(stable_id, None)
                return
            self._ensure_dirs()
            pending_path = self._pending_path(stable_id)
            with locked_json_path(pending_path):
                _atomic_write_json(
                    self._sent_path(stable_id),
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "request_id": record.request_id,
                        "operation_id": record.operation_id,
                        "sent_at": time.time(),
                        "disposition": "discarded",
                        "reason": str(reason or "interrupted"),
                    },
                )
                pending_path.unlink(missing_ok=True)

    # LLM: Terminalization after external IO is a same-epoch CAS. Sent, consumed, unknown, and
    # quarantine receipts share one durable schema and never depend on display text.
    # 函数用途: 在当前 owner/epoch 仍有效时写终态回执并移除 pending 记录。
    def mark_terminal_claimed(
        self,
        record: PendingGatewayReply,
        *,
        owner: str,
        epoch: int,
        disposition: str,
        reason: str = "",
    ) -> bool:
        if record.claim_owner != owner or record.claim_epoch != int(epoch):
            return False
        receipt: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "request_id": record.request_id,
            "operation_id": record.operation_id,
            "sent_at": time.time(),
            "disposition": str(disposition or "unknown").strip().lower(),
        }
        if reason:
            receipt["reason"] = str(reason)
        stable_id = record.stable_id
        with self._lock:
            if self.was_sent(stable_id):
                return False
            if self.root is None:
                current = self._volatile_pending.get(stable_id)
                if current != record:
                    return False
                self._volatile_sent.add(stable_id)
                self._volatile_receipts[stable_id] = dict(receipt)
                self._volatile_pending.pop(stable_id, None)
                return True
            self._ensure_dirs()
            pending_path = self._pending_path(stable_id)
            with locked_json_path(pending_path):
                if self.was_sent(stable_id):
                    return False
                current = self._read_pending(pending_path) if pending_path.exists() else None
                if current != record:
                    return False
                _atomic_write_json(self._sent_path(stable_id), receipt)
                pending_path.unlink(missing_ok=True)
                self._volatile_sent.add(stable_id)
                self._volatile_receipts[stable_id] = dict(receipt)
                return True

    # LLM: Receipt inspection is a diagnostic projection for typed terminal state, not a second
    # execution source.
    # 函数用途: 读取指定 request 的 sent/discarded/unknown/quarantined 回执。
    def terminal_receipt(self, stable_id: str) -> dict[str, object] | None:
        if self.root is None:
            receipt = self._volatile_receipts.get(stable_id)
            return dict(receipt) if receipt is not None else None
        path = self._sent_path(stable_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    # LLM: Retry backoff is a CAS transition so a stale failed sender cannot roll back a watcher
    # switch, progress cursor, or newer retry deadline written by another worker.
    # 函数用途: 最终消息发送失败后递增退避状态；记录已变化时不覆盖。
    def defer_after_failure(self, record: PendingGatewayReply) -> bool:
        attempts = record.delivery_attempts + 1
        delay = min(300.0, max(1.0, 2.0 ** min(attempts - 1, 8)))
        return self.compare_and_swap(
            record,
            replace(
                record,
                delivery_attempts=attempts,
                next_delivery_at=time.time() + delay,
            ),
        )

    def prune_sent_receipts(self, *, now: float | None = None) -> None:
        if self.root is None:
            return
        cutoff = (time.time() if now is None else now) - _SENT_RECEIPT_RETENTION_SECONDS
        with self._lock:
            self._ensure_dirs()
            for path in (self.root / "sent").glob("*.json"):
                _prune_sent_receipt(path, cutoff)

    def _ensure_dirs(self) -> None:
        assert self.root is not None
        (self.root / "pending").mkdir(parents=True, exist_ok=True)
        (self.root / "sent").mkdir(parents=True, exist_ok=True)

    def _pending_path(self, stable_id: str) -> Path:
        assert self.root is not None
        return self.root / "pending" / f"{_record_key(stable_id)}.json"

    def _sent_path(self, stable_id: str) -> Path:
        assert self.root is not None
        return self.root / "sent" / f"{_record_key(stable_id)}.json"


# LLM: Receipt reconciliation is a typed worker capability shared by the one concrete loop. This
# mixin does not own a thread, store, or callback registry and therefore creates no parallel path.
# 类用途: 集中处理 input/control receipt 与无正文终态，缩短主 worker 同时保留唯一执行循环。
class _GatewayReplyReceiptWorkerMixin:
    # LLM: Input receipt polling is typed: pending waits, queued switches watcher, consumed retires,
    # and terminal_unknown becomes a durable unknown receipt after placeholder cleanup.
    # 函数用途: 对账普通消息的稳定输入回执，并把每个终态收进持久回执。
    def _process_input_receipt(self, record: PendingGatewayReply) -> int:
        if self._poll_input_receipt is None:
            _LOGGER.error(
                "gateway input receipt watcher has no poller request_id=%s",
                record.request_id,
            )
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        try:
            state = str(self._poll_input_receipt(record) or "").strip().lower()
        except GatewayReplyQuarantineError as exc:
            self._terminalize_without_reply(
                record,
                disposition="quarantined",
                reason=exc.reason,
            )
            return 0
        except Exception as exc:
            _LOGGER.warning(
                "gateway input receipt poll failed request_id=%s error=%s",
                record.request_id,
                exc,
            )
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        if state in {"pending", "active_pending"}:
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        if state == "terminal_unknown":
            self._terminalize_without_reply(
                record,
                disposition="unknown",
                reason="active_turn_input_terminal_unknown",
            )
            return 0
        if state == "queued":
            switched = replace(record, watch_kind="request_result")
            try:
                if self.store.commit_claim(
                    record,
                    switched,
                    owner=self._claim_owner,
                    epoch=record.claim_epoch,
                    release=True,
                ) is not None:
                    self._wake.set()
            except Exception as exc:
                _LOGGER.error(
                    "gateway input receipt watcher CAS failed request_id=%s error=%s",
                    record.request_id,
                    exc,
                )
                self.store.release_claim(
                    record,
                    owner=self._claim_owner,
                    epoch=record.claim_epoch,
                )
            return 0
        if state == "consumed":
            self._terminalize_without_reply(
                record,
                disposition="discarded",
                reason="active_turn_input_consumed",
            )
            return 0
        _LOGGER.error(
            "gateway input receipt returned invalid state request_id=%s state=%s",
            record.request_id,
            state,
        )
        self._terminalize_without_reply(
            record,
            disposition="quarantined",
            reason=f"invalid_input_state:{state or 'missing'}",
        )
        return 0

    # LLM: A no-reply terminal state clears the transient provider placeholder first, then writes a
    # same-epoch unknown/discarded/quarantined receipt. Cleanup failure keeps the row retryable.
    # 函数用途: 清理占位并把无需发送正文的 watcher 收成持久终态。
    def _terminalize_without_reply(
        self,
        record: PendingGatewayReply,
        *,
        disposition: str,
        reason: str,
    ) -> bool:
        try:
            if self._discard_input_receipt is not None:
                self._discard_input_receipt(record)
        except Exception as exc:
            _LOGGER.warning(
                "gateway terminal placeholder cleanup failed request_id=%s error=%s",
                record.request_id,
                exc,
            )
            self.store.defer_claimed(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return False
        return self.store.mark_terminal_claimed(
            record,
            owner=self._claim_owner,
            epoch=record.claim_epoch,
            disposition=disposition,
            reason=reason,
        )


# LLM: Control receipt reconciliation is separated from input receipt transitions only to keep
# each capability auditable; both remain methods of the single concrete delivery worker loop.
# 类用途: 集中处理控制操作轮询、目标绑定和明确终态投递，不创建线程或第二份状态源。
class _GatewayControlReceiptWorkerMixin:
    # LLM: Unknown control delivery is reconciled only through its independent operation receipt.
    # Prepared/executing/unknown remain WAIT; only completed accepted/rejected may reach provider IO.
    # 函数用途: 轮询 `/btw` 等控制操作的稳定回执，并在明确终态后更新原占位消息。
    def _process_control_receipt(self, record: PendingGatewayReply) -> int:
        if self._poll_control_receipt is None:
            _LOGGER.error(
                "gateway control receipt watcher has no poller operation_id=%s",
                record.operation_id,
            )
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        try:
            result = self._poll_control_receipt(record)
        except GatewayReplyQuarantineError as exc:
            self._terminalize_without_reply(
                record,
                disposition="quarantined",
                reason=exc.reason,
            )
            return 0
        except Exception as exc:
            _LOGGER.warning(
                "gateway control receipt poll failed operation_id=%s error=%s",
                record.operation_id,
                exc,
            )
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        if result is None:
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        bound = self._bind_control_target(record, result)
        if bound is None:
            return 0
        record = bound
        if result.control_state == "terminal_unknown":
            if record.control_kind != "steer":
                self._terminalize_without_reply(
                    record,
                    disposition="unknown",
                    reason=f"control_{record.control_kind}_terminal_unknown",
                )
                return 0
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        if result.control_state in {"prepared", "executing"}:
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        if result.delivery_status == "unknown":
            self.store.release_claim(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        return self._deliver_control_receipt_result(record, result)

    # LLM: Only completed accepted/rejected receipts with server-authored text can cross the
    # provider boundary; failed provider IO keeps the same claimed record retryable.
    # 函数用途: 校验明确的控制终态并更新原通道占位，成功后写同 stable ID 的 sent 回执。
    def _deliver_control_receipt_result(
        self,
        record: PendingGatewayReply,
        result: GatewayControlReceiptResult,
    ) -> int:
        if result.delivery_status not in {"accepted", "rejected"}:
            self._terminalize_without_reply(
                record,
                disposition="quarantined",
                reason="invalid_control_delivery_status",
            )
            return 0
        if not result.message:
            self._terminalize_without_reply(
                record,
                disposition=result.delivery_status,
                reason="control_receipt_completed_without_message",
            )
            return 0
        try:
            delivered = bool(self._deliver_response(record, result.message))
        except Exception as exc:
            _LOGGER.warning(
                "gateway control receipt delivery failed operation_id=%s error=%s",
                record.operation_id,
                exc,
            )
            delivered = False
        if not delivered:
            self.store.defer_claimed(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
            )
            return 0
        return 1 if self._record_sent(record) else 0

    # LLM: The first non-empty target observed for a control operation is a same-epoch one-way bind.
    # Later GET responses may omit it but can never replace it with another turn.
    # 函数用途: 绑定控制操作首次确认的目标回合，并隔离 kind 或目标漂移。
    def _bind_control_target(
        self,
        record: PendingGatewayReply,
        result: GatewayControlReceiptResult,
    ) -> PendingGatewayReply | None:
        if result.control_kind and result.control_kind != record.control_kind:
            self._terminalize_without_reply(
                record,
                disposition="quarantined",
                reason="control_receipt_kind_conflict",
            )
            return None
        target_turn_id = result.target_turn_id
        if record.request_id:
            if target_turn_id and target_turn_id != record.request_id:
                self._terminalize_without_reply(
                    record,
                    disposition="quarantined",
                    reason="control_receipt_target_conflict",
                )
                return None
            return record
        if not target_turn_id:
            return record
        return self.store.commit_claim(
            record,
            replace(record, request_id=target_turn_id),
            owner=self._claim_owner,
            epoch=record.claim_epoch,
            release=False,
        )

# LLM: Worker 永久轮询既有 request_id，模型耗时不设 60 秒终止窗；投递失败只退避重试发送。
# 类用途: 在一个后台线程中恢复并送达待回送结果，成功后写回执防止重发。
class GatewayReplyDeliveryWorker(
    _GatewayReplyReceiptWorkerMixin,
    _GatewayControlReceiptWorkerMixin,
):
    """一个可恢复轮询线程，不对模型响应设置期限。"""

    def __init__(
        self,
        store: GatewayReplyDeliveryStore,
        *,
        poll_response: Callable[[PendingGatewayReply], str | None],
        deliver_response: Callable[[PendingGatewayReply, str], bool],
        poll_progress: Callable[[PendingGatewayReply], tuple[list[str], int]] | None = None,
        deliver_progress: Callable[[PendingGatewayReply, str], bool] | None = None,
        receipt_callbacks: GatewayReplyReceiptCallbacks | None = None,
        poll_interval: float = 1.0,
        lease: GatewayClaimLeaseConfig | None = None,
    ) -> None:
        receipt_callbacks = receipt_callbacks or GatewayReplyReceiptCallbacks()
        lease = lease or GatewayClaimLeaseConfig()
        self.store = store
        self._poll_response = poll_response
        self._deliver_response = deliver_response
        self._poll_progress = poll_progress
        self._deliver_progress = deliver_progress
        self._poll_input_receipt = receipt_callbacks.poll_input
        self._poll_control_receipt = receipt_callbacks.poll_control
        self._discard_input_receipt = receipt_callbacks.clear_placeholder
        self._poll_interval = max(0.01, float(poll_interval))
        self._claim_owner = lease.owner or (
            f"adapter-reply:{os.getpid()}:{uuid.uuid4().hex}"
        )
        self._claim_ttl = lease.ttl_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # LLM: Enqueue is put-if-absent; the caller receives the authoritative existing record so a
    # duplicate inbound callback cannot replace another user's route or progress handle.
    # 函数用途: 登记一条持久 watcher 并唤醒后台线程，返回权威记录和是否首次创建。
    def enqueue(
        self,
        record: PendingGatewayReply,
    ) -> tuple[PendingGatewayReply | None, bool]:
        current, created = self.store.put_if_absent(record)
        self._wake.set()
        return current, created

    # LLM: Serialize discard against the delivery poll so an interrupted reply cannot race into the adapter.
    # 函数用途: 在投递锁内同步退役匹配的待回送记录。
    def discard_where(
        self,
        predicate: Callable[[PendingGatewayReply], bool],
        *,
        reason: str,
    ) -> list[PendingGatewayReply]:
        """Synchronously retire matching replies, excluding an in-flight delivery race."""
        discarded: list[PendingGatewayReply] = []
        with self._run_lock:
            for record in self.store.pending():
                if not predicate(record):
                    continue
                self.store.mark_discarded(record, reason=reason)
                discarded.append(record)
        return discarded

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self.store.prune_sent_receipts()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="gateway-reply-delivery", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def run_once(self) -> int:
        """Poll and deliver each currently ready record once. Useful for deterministic tests too."""
        if not self._run_lock.acquire(blocking=False):
            return 0
        try:
            now = time.time()
            return sum(self._process_record(record, now) for record in self.store.pending())
        finally:
            self._run_lock.release()

    def _process_record(self, record: PendingGatewayReply, now: float) -> int:
        if self._stop.is_set() or record.next_delivery_at > now:
            return 0
        claimed = self.store.claim(
            record,
            owner=self._claim_owner,
            now=now,
            ttl=self._claim_ttl,
        )
        if claimed is None:
            return 0
        if claimed.watch_kind == "input_receipt":
            return self._process_input_receipt(claimed)
        if claimed.watch_kind == "control_receipt":
            return self._process_control_receipt(claimed)
        try:
            progressed = self._deliver_available_progress(claimed)
        except GatewayReplyQuarantineError as exc:
            self._terminalize_without_reply(
                claimed,
                disposition="quarantined",
                reason=exc.reason,
            )
            return 0
        if progressed is None:
            return 0
        claimed = progressed
        try:
            response = self._poll_response(claimed)
        except GatewayReplyQuarantineError as exc:
            self._terminalize_without_reply(
                claimed,
                disposition="quarantined",
                reason=exc.reason,
            )
            return 0
        except Exception as exc:
            _LOGGER.warning("gateway reply poll failed request_id=%s error=%s", claimed.request_id, exc)
            self.store.release_claim(
                claimed,
                owner=self._claim_owner,
                epoch=claimed.claim_epoch,
            )
            return 0
        if response is None:
            self.store.release_claim(
                claimed,
                owner=self._claim_owner,
                epoch=claimed.claim_epoch,
            )
            return 0
        if self.store.was_sent(claimed.stable_id):
            return 0
        try:
            sent = bool(self._deliver_response(claimed, response))
        except Exception as exc:
            _LOGGER.warning("gateway reply delivery failed request_id=%s error=%s", claimed.request_id, exc)
            sent = False
        if not sent:
            self.store.defer_claimed(
                claimed,
                owner=self._claim_owner,
                epoch=claimed.claim_epoch,
            )
            return 0
        return 1 if self._record_sent(claimed) else 0

    def _deliver_available_progress(
        self,
        record: PendingGatewayReply,
    ) -> PendingGatewayReply | None:
        if self._poll_progress is None or self._deliver_progress is None:
            return record
        try:
            messages, next_cursor = self._poll_progress(record)
        except GatewayReplyQuarantineError:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "gateway progress poll failed request_id=%s error=%s",
                record.request_id,
                exc,
            )
            return record
        if messages:
            try:
                sent = bool(self._deliver_progress(record, "\n".join(messages)))
            except Exception as exc:
                _LOGGER.warning(
                    "gateway progress delivery failed request_id=%s error=%s",
                    record.request_id,
                    exc,
                )
                sent = False
            if not sent:
                # Commentary/tool progress is presentation-only and at-most-once.
                # Retrying it every poll can flood a broken IM route and must not
                # delay the durable final response.
                updated = replace(record, progress_cursor=next_cursor)
                return self.store.commit_claim(
                    record,
                    updated,
                    owner=self._claim_owner,
                    epoch=record.claim_epoch,
                    release=False,
                )
        if next_cursor <= record.progress_cursor:
            return record
        updated = replace(record, progress_cursor=next_cursor)
        return self.store.commit_claim(
            record,
            updated,
            owner=self._claim_owner,
            epoch=record.claim_epoch,
            release=False,
        )

    # LLM: Provider acceptance is recorded only under the same claim epoch that performed delivery.
    # 函数用途: 持久写入 sent 回执；旧 epoch 被接管时不覆盖新状态。
    def _record_sent(self, record: PendingGatewayReply) -> bool:
        try:
            return self.store.mark_terminal_claimed(
                record,
                owner=self._claim_owner,
                epoch=record.claim_epoch,
                disposition="sent",
            )
        except OSError as exc:
            # 外部通道已接受消息；本进程内必须防重发，同时把落回执故障明确写日志。
            _LOGGER.error(
                "channel reply sent but receipt persistence failed request_id=%s error=%s",
                record.request_id,
                exc,
            )
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._wake.wait(self._poll_interval)
            self._wake.clear()


# LLM: Immutable route comparison covers stable id, one-way watch transition, operation receipt,
# kind, and the full external issuer scope. Only same-epoch control commit may bind an empty target.
# 函数用途: 校验稳定请求的通道、用户、会话和群聊身份不漂移，并禁止目标回合绑定后改变。
def _ensure_same_pending_route(
    current: PendingGatewayReply,
    candidate: PendingGatewayReply,
    *,
    allow_control_target_bind: bool = False,
    allow_initial_watch_replay: bool = False,
    allow_unbound_control_replay: bool = False,
) -> None:
    watch_matches = current.watch_kind == candidate.watch_kind
    if current.watch_kind == "input_receipt" and candidate.watch_kind == "request_result":
        watch_matches = True
    if (
        allow_initial_watch_replay
        and current.watch_kind == "request_result"
        and candidate.watch_kind == "input_receipt"
    ):
        watch_matches = True
    target_matches = current.request_id == candidate.request_id
    if (
        allow_control_target_bind
        and current.watch_kind == "control_receipt"
        and not current.request_id
        and bool(candidate.request_id)
    ):
        target_matches = True
    if (
        allow_unbound_control_replay
        and current.watch_kind == "control_receipt"
        and bool(current.request_id)
        and not candidate.request_id
    ):
        target_matches = True
    current_route = (
        current.stable_id,
        current.operation_id,
        current.receipt_id,
        current.control_kind,
        current.channel,
        current.user_id,
        current.message_id,
        current.conversation_id,
        current.channel_chat_type,
        current.channel_chat_id,
    )
    candidate_route = (
        candidate.stable_id,
        candidate.operation_id,
        candidate.receipt_id,
        candidate.control_kind,
        candidate.channel,
        candidate.user_id,
        candidate.message_id,
        candidate.conversation_id,
        candidate.channel_chat_type,
        candidate.channel_chat_id,
    )
    if current_route != candidate_route or not target_matches or not watch_matches:
        raise ValueError(
            f"pending gateway reply route conflict for request_id={candidate.request_id}"
        )


def _record_key(stable_id: str) -> str:
    return hashlib.sha256(stable_id.encode("utf-8", "replace")).hexdigest()


def _prune_sent_receipt(path: Path, cutoff: float) -> None:
    try:
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
    except OSError as exc:
        _LOGGER.warning("cannot prune channel delivery receipt path=%s error=%s", path, exc)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        with temp_path.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


__all__ = [
    "GatewayClaimLeaseConfig",
    "GatewayControlReceiptResult",
    "GatewayReplyDeliveryStore",
    "GatewayReplyDeliveryWorker",
    "GatewayReplyQuarantineError",
    "GatewayReplyReceiptCallbacks",
    "PendingGatewayReply",
]
