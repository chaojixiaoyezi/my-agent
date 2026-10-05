from __future__ import annotations

"""LLM: Gateway 负责执行请求；本模块在原 pending/sent 中记录 dispatch 意图、进程身份和 epoch，禁止 TTL 直接授权重发。

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
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..common.json_io import locked_json_path

_LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = 7
# 已外发回执正文保留 7 天：足够排查与重放，又不长期占用会话存储。
_SENT_RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60
# 认领一次外部投递的默认租约 120 秒；已开始外发时还必须核验旧执行者，过期不等于死亡。
_DEFAULT_CLAIM_TTL_SECONDS = 120.0
# 投递循环同类异常日志的限频窗口 60 秒：轮询约每秒一轮，持续失败时同一异常在窗口内只记一次，
# 既保留"还在失败"的可观测性，又不把日志刷爆。
_LOOP_ERROR_LOG_INTERVAL_SECONDS = 60.0
_PENDING_WATCH_KINDS = frozenset(
    {"input_receipt", "request_result", "control_receipt"}
)
_CONTROL_RECEIPT_STATES = frozenset(
    {"prepared", "executing", "completed", "terminal_unknown"}
)
# LLM: 隔离类别是结构化字段：manager 在生成隔离时按 HTTP 状态码 + 服务端 error_code 判定，delivery 只读它，
#   不再拿原因字符串做正则匹配（AGENTS.md：机器判断只用结构化事实）。`auth` 才给用户可见回复。
QUARANTINE_CATEGORY_AUTH = "auth"
QUARANTINE_CATEGORY_CONFIG = "config"


# LLM: 新字段损坏时不能退回“未外发”；旧 schema 的空标记仍按原路径读取。
# 函数用途: 校验当前 pending 的外发意图，防止丢身份后错误重发。
def _validated_dispatch(value: object, epoch: int) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid gateway reply dispatch marker")
    if not value:
        return {}
    marker = dict(value)
    if marker.get("state") not in {"dispatch-started", "retryable"}:
        raise ValueError("invalid gateway reply dispatch state")
    marker_epoch = int(marker.get("claim_epoch") or 0)
    if not 0 < marker_epoch <= epoch or not marker.get("owner"):
        raise ValueError("invalid gateway reply dispatch claim")
    if not marker.get("message_key") or not marker.get("message_sequence"):
        raise ValueError("missing gateway reply dispatch message identity")
    digest = str(marker.get("payload_sha256") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid gateway reply dispatch payload digest")
    return marker


# LLM: TTL 与 attempt 死亡是不同事实；结束后明确可重试的标记由当前 worker 持久提交，不靠墙钟猜。
# 函数用途: 决定旧外发是否阻止另一 worker 领取。
def _dispatch_claim_blocked(record: PendingGatewayReply) -> bool:
    marker = record.dispatch
    if not marker or marker.get("state") == "retryable":
        return False
    return _dispatch_owner_live(marker.get("owner_process")) is not False


# LLM: 原进程核验的 is_pid_alive 把所有 OSError 当死；先排除权限/不可读，再复用 host/PID/start_time 判定。
# 函数用途: 只在当前进程域有明确死亡证据时允许接管，异常与字段缺失一律未知。
def _dispatch_owner_live(identity: object) -> bool | None:
    from ..gateway_parts.daemon_metadata import process_host_id, process_identity_is_live

    if not isinstance(identity, dict) or identity.get("host_id") != process_host_id():
        return None
    try:
        pid = int(identity.get("pid") or 0)
        if pid <= 0:
            return None
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return None
    try:
        return process_identity_is_live(identity)
    except Exception:
        return None


# LLM: 能力只取回调所属 adapter 或其唯一注册表，不按 channel 名/方法文本猜支持。
# 函数用途: 在不改 ChannelManager 接线的情况下取它已注册的实际 adapter；缺声明按不支持。
def _delivery_adapter(callback: object, channel: str) -> object | None:
    receiver = getattr(callback, "__self__", None)
    registry = getattr(receiver, "_delivery_registry", None)
    if registry is not None:
        return registry.adapter_for(channel)
    return receiver if receiver is not None else getattr(callback, "delivery_adapter", None)


# LLM: provider 去重可能只有有限窗口；时间倒退或超过首次 dispatch 的窗口都不能盲目重发。
# 函数用途: 核对原标记是否仍可用渠道稳定键安全重放。
def _provider_replay_allowed(adapter: object, marker: dict[str, object]) -> bool:
    if getattr(adapter, "provider_idempotent_delivery", False) is not True:
        return False
    window = float(getattr(adapter, "provider_idempotency_window_seconds", 0.0) or 0.0)
    elapsed = time.time() - float(marker.get("started_at") or 0.0)
    return elapsed >= 0 and (window <= 0 or elapsed < window)


# LLM: message key 复用 manager 的既有结构生成器，不带 claim_epoch；epoch 只用于本地栅栏。
# 函数用途: 为当前逻辑消息构造外发前落盘的意图，进度与最终回复独立。
def _dispatch_marker(record: PendingGatewayReply, text: str, message: tuple[str, int]) -> dict[str, object]:
    from ..gateway_parts.daemon_metadata import build_process_identity
    from .manager import _gateway_delivery_key

    sequence, next_cursor = message
    phase = "progress" if sequence.startswith("progress:") else "final"
    return {
        "state": "dispatch-started", "owner": record.claim_owner,
        "claim_epoch": record.claim_epoch, "owner_process": build_process_identity(),
        "message_sequence": sequence, "next_cursor": next_cursor,
        "message_key": _gateway_delivery_key(
            message_id=record.message_id, request_id=record.stable_id,
            phase=phase, progress_cursor=record.progress_cursor,
        ),
        "payload_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "started_at": record.dispatch.get("started_at", time.time()),
    }


# LLM: 用户可见文案按结构化原因码二选一；不解析服务端 message，也不把内部 reason 串露出给用户。
# 函数用途: 给鉴权拒绝生成一句中文的、告诉用户怎么办的提示。
def _gateway_denial_text(denial_code: str) -> str:
    if str(denial_code or "").strip() == "LOCAL_CREDENTIAL_REQUIRED":
        return "Gateway 拒绝了这个请求：本机凭据无效或缺失。请重启 my-agent 让它重新读取凭据。"
    return "Gateway 拒绝了这个请求（鉴权失败）。请重启 my-agent；若仍失败，检查本机凭据。"


# LLM: Auth/config failures are typed transport quarantine signals, never user-visible reply text.
# 类用途: 通知回送 worker 持久隔离无法继续对账的 Gateway watcher。
class GatewayReplyQuarantineError(RuntimeError):
    # LLM: category 只承载结构化类别（auth/config，见 QUARANTINE_CATEGORY_*）；denial_code 只承载服务端
    #   结构化 error_code（g2bfix1 约定的 LOCAL_CREDENTIAL_REQUIRED）。两者都缺省，缺省时按非 auth 处理。
    #   "要不要给用户回一句"由 delivery 的 _terminalize_quarantine 直接读 self.category 判定，
    #   这里不再提供字符串匹配的派生属性（AGENTS.md：机器判断只用结构化事实，且不留双份判据）。
    # 函数用途: 记录一次隔离的类别、原因与服务端原因码。
    def __init__(self, reason: str, denial_code: str = "", category: str = "") -> None:
        self.reason = str(reason or "gateway_reply_quarantine")
        self.denial_code = str(denial_code or "").strip()
        self.category = str(category or "").strip().lower()
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


# LLM: PendingGatewayReply 只保存回送身份及当前外发意图；正文只保存 SHA256，恢复前核同一消息序号和 epoch。
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
    dispatch: dict[str, object] = field(default_factory=dict)

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
        object.__setattr__(self, "dispatch", _validated_dispatch(self.dispatch, claim_epoch))

    # LLM: schema 7 仅在原记录增加 dispatch；不迁移路径，不保存正文或新的平行回执。
    # 函数用途: 把当前路由、租约和外发标记一起序列化。
    def to_dict(self) -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, **asdict(self)}

    # LLM: 旧记录无 dispatch 时按尚未外发读取；损坏的新标记必须拒绝，不能丢标记后重发。
    # 函数用途: 从原 pending JSON 恢复同一投递状态。
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
            dispatch=data.get("dispatch", {}),
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
    # LLM: claim 在原文件锁内递增 epoch；dispatch-started 即使已过期也要旧进程明确死亡才可接管。
    # 函数用途: 核验旧外发执行者后领取回送租约，不在锁内执行渠道 IO。
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
                if _dispatch_claim_blocked(current):
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
                if _dispatch_claim_blocked(current):
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

    # LLM: 原 sent 回执保留逻辑消息序号和实际 claim epoch；旧 owner/epoch 不能覆盖新的 unknown/sent。
    # 函数用途: 以同一文件 CAS 提交外发终态及其消息身份，成功后删除 pending。
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
            "claim_epoch": record.claim_epoch,
            "message_sequence": str(record.dispatch.get("message_sequence") or ""),
            "message_key": str(record.dispatch.get("message_key") or ""),
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


# LLM: 隔离收口与"无正文终态"是所有回送状态机共用的能力（input / control / 普通回复三条路都调）；
#   单独成一个 mixin，既让每类能力的尺寸可审计，也避免把 receipt 状态机和隔离语义堆进同一个类。
# 类用途: 提供"清理占位并写终态"、"按结构化类别收口隔离"和"包装可能抛隔离的步骤"三个共用方法。
class _GatewayQuarantineTerminalizeMixin:
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

    # LLM: 隔离的统一收口：auth 类（构造时按状态码 + error_code 定的结构化类别，见 QUARANTINE_CATEGORY_*）
    #   先给用户一句可见原因，再写终态；其它隔离保持静默（清占位 + 写 quarantined），两种语义同时成立，
    #   互不覆盖。发不出去也不改变终态结果。判据只读 category 字段，不做原因字符串匹配。
    # 函数用途: 收口一条隔离记录，鉴权拒绝额外回用户一条失败原因。
    def _terminalize_quarantine(
        self,
        record: PendingGatewayReply,
        *,
        reason: str,
        denial_code: str = "",
        category: str = "",
    ) -> bool:
        if str(category or "").strip().lower() == QUARANTINE_CATEGORY_AUTH:
            try:
                record, _sent = self._dispatch_message(record, _gateway_denial_text(denial_code), ("final", 0))
                if record is None:
                    return False
            except Exception as exc:
                _LOGGER.warning(
                    "gateway auth denial reply failed request_id=%s error=%s",
                    record.request_id,
                    exc,
                )
        return self._terminalize_without_reply(
            record,
            disposition="quarantined",
            reason=reason,
        )

    # LLM: 把"可能抛隔离的步骤"包一层：隔离一律收成终态（auth 类额外回用户一句），调用方按
    #   返回的 quarantined 决定是否继续；step_name 只用于日志分阶段，不做业务判断。
    # 函数用途: 执行一个可能抛隔离的步骤，返回 (结果, 是否已收口)；非隔离异常释放认领供稍后重试。
    def _quarantine_aware(self, step, record: PendingGatewayReply, step_name: str):
        try:
            return step(record), False
        except GatewayReplyQuarantineError as exc:
            self._terminalize_quarantine(
                record,
                reason=exc.reason,
                denial_code=exc.denial_code,
                category=exc.category,
            )
            return None, True
        except Exception as exc:  # noqa: BLE001 普通运输失败保持原有"释放认领、稍后重试"语义
            _LOGGER.warning(
                "gateway %s poll failed request_id=%s error=%s",
                step_name,
                record.request_id,
                exc,
            )
            self.store.release_claim(record, owner=self._claim_owner, epoch=record.claim_epoch)
            return None, True


# LLM: Receipt reconciliation is a typed worker capability shared by the one concrete loop. This
# mixin does not own a thread, store, or callback registry and therefore creates no parallel path.
# 类用途: 集中处理 input receipt 状态机与无正文终态，缩短主 worker 同时保留唯一执行循环。
class _GatewayReplyReceiptWorkerMixin(_GatewayQuarantineTerminalizeMixin):
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
            self._terminalize_quarantine(
                record,
                reason=exc.reason,
                denial_code=exc.denial_code,
                category=exc.category,
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

    # LLM: 无正文终态、隔离收口与"可能抛隔离的步骤"包装统一放在 _GatewayQuarantineTerminalizeMixin
    #   （见下方该类）；本 mixin 只保留 input receipt 的对账状态机，避免单一 mixin 越过尺寸阈值。


# LLM: Control receipt reconciliation is separated from input receipt transitions only to keep
# each capability auditable; both remain methods of the single concrete delivery worker loop.
# 类用途: 集中处理控制操作轮询、目标绑定和明确终态投递，不创建线程或第二份状态源。
class _GatewayControlReceiptWorkerMixin(_GatewayQuarantineTerminalizeMixin):
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
            self._terminalize_quarantine(
                record,
                reason=exc.reason,
                denial_code=exc.denial_code,
                category=exc.category,
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
        return self._deliver_final(record, result.message)

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

# LLM: 共用原 worker/store，以消息序号持久意图后才调用渠道；恢复必须先核验进程与 provider 能力。
# 类用途: 封装最终/进度消息的外发栅栏和 unknown 收口，不引入另一套线程或账本。
class _GatewayReplyDispatchWorkerMixin:
    # LLM: progress 和 final 分别消费各自已配置的 callback，能力声明来自该 callback 的宿主注册事实。
    # 函数用途: 按结构化消息序号取当前渠道发送入口。
    def _dispatch_callback(self, sequence: str):
        return self._deliver_progress if sequence.startswith("progress:") else self._deliver_response

    # LLM: marker 的 owner 已在 claim 中确认死亡；无幂等/查询能力时绝不调用第二次渠道发送。
    # 函数用途: 对账旧外发意图，返回可继续的记录或已收口的终态计数。
    def _reconcile_dispatch(self, record: PendingGatewayReply) -> tuple[PendingGatewayReply | None, int]:
        marker = record.dispatch
        if not marker:
            return record, 0
        adapter = _delivery_adapter(self._dispatch_callback(str(marker["message_sequence"])), record.channel)
        confirmed = self._query_dispatch(adapter, str(marker["message_key"]))
        if confirmed is True:
            return self._acknowledge_dispatch(record)
        if confirmed is False:
            updated = replace(record, dispatch={**marker, "state": "retryable", "retry_reason": "not_started"})
            return self.store.commit_claim(record, updated, owner=self._claim_owner, epoch=record.claim_epoch, release=False), 0
        if marker.get("retry_reason") == "not_started" or _provider_replay_allowed(adapter, marker):
            return record, 0
        self._terminalize_without_reply(record, disposition="unknown", reason="channel_dispatch_unverifiable")
        return None, 0

    # LLM: 查询是显式声明的只读能力；只接受 bool，缺接口、异常及模糊结果一律未知。
    # 函数用途: 用稳定 message key 查询外部消息是否已接受。
    def _query_dispatch(self, adapter: object, key: str) -> bool | None:
        if getattr(adapter, "provider_delivery_queryable", False) is not True:
            return None
        query = getattr(adapter, "query_delivery", None)
        if not callable(query):
            return None
        try:
            result = query(key)
            return result if type(result) is bool else None
        except Exception:
            return None

    # LLM: 已查询确认的 progress 只推进原游标；final 才写 sent 终态，两种身份不互相消费。
    # 函数用途: 不重发消息，把查询回执提交到原 pending/sent。
    def _acknowledge_dispatch(self, record: PendingGatewayReply) -> tuple[PendingGatewayReply | None, int]:
        if record.dispatch["message_sequence"] == "final":
            return None, int(self._record_sent(record))
        updated = replace(record, progress_cursor=int(record.dispatch["next_cursor"]), dispatch={})
        return self.store.commit_claim(record, updated, owner=self._claim_owner, epoch=record.claim_epoch, release=False), 0

    # LLM: 重放必须保持同一逻辑序号和正文摘要；新 epoch 只替换本地 owner，不改变 provider message key。
    # 函数用途: 同 epoch 持久写 dispatch-started；落盘失败或旧 claim 失权时不触发外发。
    def _start_dispatch(self, record: PendingGatewayReply, text: str, message: tuple[str, int]) -> PendingGatewayReply | None:
        marker = _dispatch_marker(record, text, message)
        if record.dispatch and (
            record.dispatch["message_sequence"] != marker["message_sequence"]
            or record.dispatch["payload_sha256"] != marker["payload_sha256"]
            or record.dispatch["message_key"] != marker["message_key"]
        ):
            self._terminalize_without_reply(record, disposition="unknown", reason="channel_dispatch_payload_changed")
            return None
        return self.store.commit_claim(
            record, replace(record, dispatch=marker), owner=self._claim_owner,
            epoch=record.claim_epoch, release=False,
        )

    # LLM: callback 不持 store 锁；BaseException 模拟/真实退出保持标记，普通异常只成为未确认回执。
    # 函数用途: 先写外发意图，再调用原渠道 callback，返回实际使用的 claim 快照。
    def _dispatch_message(self, record: PendingGatewayReply, text: str, message: tuple[str, int]) -> tuple[PendingGatewayReply | None, bool]:
        started = self._start_dispatch(record, text, message)
        if started is None:
            return None, False
        try:
            return started, bool(self._dispatch_callback(message[0])(started, text))
        except Exception as exc:
            _LOGGER.warning("gateway reply dispatch failed request_id=%s error=%s", record.request_id, exc)
            return started, False

    # LLM: bool False 不证明未发生；只有仍在去重窗口内的 provider 才能退避原键重发，其余收 unknown。
    # 函数用途: 明确记录当前发送尝试已返回，决定安全退避或停止自动重发。
    def _dispatch_failed(self, record: PendingGatewayReply) -> None:
        adapter = _delivery_adapter(self._deliver_response, record.channel)
        if not _provider_replay_allowed(adapter, record.dispatch):
            self._terminalize_without_reply(record, disposition="unknown", reason="channel_dispatch_result_unknown")
            return
        finished = replace(record, dispatch={**record.dispatch, "state": "retryable", "retry_reason": "provider_idempotent"})
        committed = self.store.commit_claim(record, finished, owner=self._claim_owner, epoch=record.claim_epoch, release=False)
        if committed is not None:
            self.store.defer_claimed(committed, owner=self._claim_owner, epoch=committed.claim_epoch)

    # LLM: 普通最终回复和控制回执共用同一外发栅栏，sent 回执必须提交实际调用渠道的快照。
    # 函数用途: 安全发送一次最终消息并写入同 epoch 终态。
    def _deliver_final(self, record: PendingGatewayReply, text: str) -> int:
        started, sent = self._dispatch_message(record, text, ("final", 0))
        if started is None:
            return 0
        if not sent:
            self._dispatch_failed(started)
            return 0
        return int(self._record_sent(started))

    # LLM: 已发送进度的游标写入偶发失败时，仅原 attempt 立即重试同 epoch 本地提交一次，不再调用 provider。
    # 函数用途: 原子推进进度游标并清除当前外发意图；持续写失败仍保留可恢复标记。
    def _commit_progress(self, record: PendingGatewayReply, next_cursor: int) -> PendingGatewayReply | None:
        updated = replace(record, progress_cursor=next_cursor, dispatch={})
        try:
            return self.store.commit_claim(record, updated, owner=self._claim_owner, epoch=record.claim_epoch, release=False)
        except OSError:
            return self.store.commit_claim(record, updated, owner=self._claim_owner, epoch=record.claim_epoch, release=False)


# LLM: Worker 永久轮询既有 request_id，模型耗时不设 60 秒终止窗；外发恢复沿 dispatch 合同而非盲目退避。
# 类用途: 在一个后台线程中恢复并送达待回送结果，成功后写回执防止重发。
class GatewayReplyDeliveryWorker(
    _GatewayReplyReceiptWorkerMixin,
    _GatewayControlReceiptWorkerMixin,
    _GatewayReplyDispatchWorkerMixin,
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
        # 投递循环异常日志的限频状态：只在投递线程内访问，不需要加锁。
        self._loop_error_last = ""
        self._loop_error_log_at = 0.0
        self._loop_error_suppressed = 0

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

    # LLM: 外发意图在新 claim 后、所有轮询/渠道副作用前对账；旧进程未知时 claim 已短路。
    # 函数用途: 沿唯一 worker 入口恢复旧意图，再处理输入、控制或最终消息。
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
        claimed, reconciled = self._reconcile_dispatch(claimed)
        if claimed is None:
            return reconciled
        if claimed.watch_kind == "input_receipt":
            return self._process_input_receipt(claimed)
        if claimed.watch_kind == "control_receipt":
            return self._process_control_receipt(claimed)
        polled = self._poll_progress_then_response(claimed)
        if polled is None:
            return 0
        claimed, response = polled
        if self.store.was_sent(claimed.stable_id):
            return 0
        return self._deliver_final(claimed, response)

    # LLM: 先把进度快照送达并推进游标，再取最终回复；任一步隔离已收口或归还认领时返回 None，
    #   调用方直接结束本次处理，不进入投递。
    # 函数用途: 依次跑 progress 与 response 两步轮询，返回 (记录, 最终正文) 或 None。
    def _poll_progress_then_response(
        self, claimed: PendingGatewayReply
    ) -> tuple[PendingGatewayReply, str] | None:
        progressed, quarantined = self._quarantine_aware(
            self._deliver_available_progress, claimed, "progress"
        )
        if quarantined or progressed is None:
            return None
        response, quarantined = self._quarantine_aware(self._poll_response, progressed, "response")
        if quarantined:
            return None
        if response is None:
            self.store.release_claim(
                progressed,
                owner=self._claim_owner,
                epoch=progressed.claim_epoch,
            )
            return None
        return progressed, response

    # LLM: progress 先写独立 dispatch 意图；原 attempt 的确认只推进游标，final 的恢复不再次轮询进度。
    # 函数用途: 投递一个进度批次，游标提交偶发失败只重试本地 CAS，不重复外发。
    def _deliver_available_progress(
        self,
        record: PendingGatewayReply,
    ) -> PendingGatewayReply | None:
        if self._poll_progress is None or self._deliver_progress is None:
            return record
        if record.dispatch.get("message_sequence") == "final":
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
            record, _sent = self._dispatch_message(record, "\n".join(messages), (f"progress:{record.progress_cursor}", next_cursor))
            if record is None:
                return None
            # 原 attempt 收到返回后按原 at-most-once 合同消费进度，不因 False 反复刷屏。
        if next_cursor <= record.progress_cursor:
            return record
        return self._commit_progress(record, next_cursor)

    # LLM: sent 只提交实际 dispatch 快照；失败保留 dispatch，活进程/未知进程不能重领并再次发送。
    # 函数用途: 持久写同 epoch sent 回执；写失败不丢外发意图，不把旧回执覆盖到新状态。
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
            # LLM: 单轮意外异常（例如持续落盘失败时 release_claim 抛错）不能让投递线程静默退出——
            #   线程一死，之后所有通道回复都停了，而且没有任何信号。这里记 warning 并按轮询间隔退避后继续，
            #   既保留可观测性又不打满 CPU；停止请求仍然生效。
            #   InterruptedError 是项目约定的停止信号（OSError 子类），必须显式放行交给上层，不能被兜底吞掉；
            #   KeyboardInterrupt/SystemExit 等 BaseException 同样穿过本兜底。BlockingIOError 不放行：
            #   本项目用它表示“锁正忙”（json_io 非阻塞锁、目录锁），是临时状态——放行会让一次锁冲突
            #   就永久停掉投递线程，所以按普通单轮异常记录（限频）后下一轮再试。
            try:
                self.run_once()
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 投递线程必须活着，异常只记录不上抛
                self._log_loop_error(exc)
            self._wake.wait(self._poll_interval)
            self._wake.clear()

    # LLM: 同类异常按时间窗限频：轮询约每秒一轮，持续失败时不能一秒一条 warning 刷日志；
    #   窗口内相同异常只累计计数，下一次真正记录时先输出被抑制的次数。只在投递线程内调用，不加锁。
    # 函数用途: 记录投递循环的意外异常，并对持续重复的同一异常限频。
    def _log_loop_error(self, exc: BaseException) -> None:
        now = time.monotonic()
        message = f"{type(exc).__name__}: {exc}"
        if message == self._loop_error_last and now - self._loop_error_log_at < _LOOP_ERROR_LOG_INTERVAL_SECONDS:
            self._loop_error_suppressed += 1
            return
        if self._loop_error_suppressed:
            _LOGGER.warning(
                "gateway reply delivery loop error repeated %d more times: %s",
                self._loop_error_suppressed,
                self._loop_error_last,
            )
        _LOGGER.warning("gateway reply delivery loop error: %s", message)
        self._loop_error_last = message
        self._loop_error_log_at = now
        self._loop_error_suppressed = 0


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
