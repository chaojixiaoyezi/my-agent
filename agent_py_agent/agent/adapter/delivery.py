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
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = 1
_SENT_RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60


# LLM: PendingGatewayReply 只保存回送所需身份，不保存 prompt、回复正文或凭据。
# 类用途: 表示一个已经提交 Gateway、等待送回原通道的回复。
@dataclass(frozen=True)
class PendingGatewayReply:
    request_id: str
    channel: str
    user_id: str
    message_id: str
    conversation_id: str = ""
    progress_handle: str = ""
    created_at: float = 0.0
    delivery_attempts: int = 0
    next_delivery_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PendingGatewayReply:
        request_id = str(data.get("request_id") or "").strip()
        channel = str(data.get("channel") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        if not (request_id and channel and user_id):
            raise ValueError("pending gateway reply is missing request/channel/user identity")
        return cls(
            request_id=request_id,
            channel=channel,
            user_id=user_id,
            message_id=str(data.get("message_id") or ""),
            conversation_id=str(data.get("conversation_id") or ""),
            progress_handle=str(data.get("progress_handle") or ""),
            created_at=float(data.get("created_at") or 0.0),
            delivery_attempts=max(0, int(data.get("delivery_attempts") or 0)),
            next_delivery_at=max(0.0, float(data.get("next_delivery_at") or 0.0)),
        )


# LLM: Store 用原子 JSON + 短期 sent receipt 做重启恢复；不能把 pending 当成重新执行任务的队列。
# 类用途: 保存待回送记录和已发送回执，进程重启后可继续同一个 Gateway 请求。
class GatewayReplyDeliveryStore:
    """原子 pending 记录和短期 sent 回执。"""

    def __init__(self, root: Path | None) -> None:
        self.root = Path(root).expanduser() if root is not None else None
        self._volatile_pending: dict[str, PendingGatewayReply] = {}
        self._volatile_sent: set[str] = set()
        self._lock = threading.RLock()

    @property
    def durable(self) -> bool:
        return self.root is not None

    def put(self, record: PendingGatewayReply) -> None:
        with self._lock:
            if self.was_sent(record.request_id):
                return
            if self.root is None:
                self._volatile_pending[record.request_id] = record
                return
            self._ensure_dirs()
            _atomic_write_json(self._pending_path(record.request_id), record.to_dict())

    def pending(self) -> list[PendingGatewayReply]:
        with self._lock:
            if self.root is None:
                return sorted(self._volatile_pending.values(), key=lambda item: (item.created_at, item.request_id))
            self._ensure_dirs()
            records = [
                record
                for path in sorted((self.root / "pending").glob("*.json"))
                if (record := self._read_pending(path)) is not None
            ]
            return sorted(records, key=lambda item: (item.created_at, item.request_id))

    def _read_pending(self, path: Path) -> PendingGatewayReply | None:
        try:
            record = PendingGatewayReply.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if self.was_sent(record.request_id):
                path.unlink(missing_ok=True)
                return None
            return record
        except Exception as exc:
            # 坏记录必须留在原处供运维修复，不能静默删除并永久丢失回复。
            _LOGGER.error("pending channel reply record unreadable path=%s error=%s", path, exc)
            return None

    def was_sent(self, request_id: str) -> bool:
        if request_id in self._volatile_sent:
            return True
        if self.root is None:
            return False
        return self._sent_path(request_id).exists()

    def mark_sent(self, record: PendingGatewayReply) -> None:
        with self._lock:
            # Guard the already-completed external side effect in this process even if the
            # receipt filesystem becomes unhealthy after the channel accepted the message.
            self._volatile_sent.add(record.request_id)
            if self.root is None:
                self._volatile_pending.pop(record.request_id, None)
                return
            self._ensure_dirs()
            _atomic_write_json(
                self._sent_path(record.request_id),
                {
                    "schema_version": _SCHEMA_VERSION,
                    "request_id": record.request_id,
                    "sent_at": time.time(),
                },
            )
            self._pending_path(record.request_id).unlink(missing_ok=True)

    def defer_after_failure(self, record: PendingGatewayReply) -> None:
        attempts = record.delivery_attempts + 1
        delay = min(300.0, max(1.0, 2.0 ** min(attempts - 1, 8)))
        self.put(replace(record, delivery_attempts=attempts, next_delivery_at=time.time() + delay))

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

    def _pending_path(self, request_id: str) -> Path:
        assert self.root is not None
        return self.root / "pending" / f"{_record_key(request_id)}.json"

    def _sent_path(self, request_id: str) -> Path:
        assert self.root is not None
        return self.root / "sent" / f"{_record_key(request_id)}.json"


# LLM: Worker 永久轮询既有 request_id，模型耗时不设 60 秒终止窗；投递失败只退避重试发送。
# 类用途: 在一个后台线程中恢复并送达待回送结果，成功后写回执防止重发。
class GatewayReplyDeliveryWorker:
    """一个可恢复轮询线程，不对模型响应设置期限。"""

    def __init__(
        self,
        store: GatewayReplyDeliveryStore,
        *,
        poll_response: Callable[[str], str | None],
        deliver_response: Callable[[PendingGatewayReply, str], bool],
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self._poll_response = poll_response
        self._deliver_response = deliver_response
        self._poll_interval = max(0.01, float(poll_interval))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def enqueue(self, record: PendingGatewayReply) -> None:
        self.store.put(record)
        self._wake.set()

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
        try:
            response = self._poll_response(record.request_id)
        except Exception as exc:
            _LOGGER.warning("gateway reply poll failed request_id=%s error=%s", record.request_id, exc)
            return 0
        if response is None:
            return 0
        try:
            sent = bool(self._deliver_response(record, response))
        except Exception as exc:
            _LOGGER.warning("gateway reply delivery failed request_id=%s error=%s", record.request_id, exc)
            sent = False
        if not sent:
            self.store.defer_after_failure(record)
            return 0
        self._record_sent(record)
        return 1

    def _record_sent(self, record: PendingGatewayReply) -> None:
        try:
            self.store.mark_sent(record)
        except OSError as exc:
            # 外部通道已接受消息；本进程内必须防重发，同时把落回执故障明确写日志。
            _LOGGER.error(
                "channel reply sent but receipt persistence failed request_id=%s error=%s",
                record.request_id,
                exc,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._wake.wait(self._poll_interval)
            self._wake.clear()


def _record_key(request_id: str) -> str:
    return hashlib.sha256(request_id.encode("utf-8", "replace")).hexdigest()


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
    "GatewayReplyDeliveryStore",
    "GatewayReplyDeliveryWorker",
    "PendingGatewayReply",
]
