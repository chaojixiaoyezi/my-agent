"""飞书私聊解锁前的短期待续送消息队列。

锁定消息仍保留原 IncomingMessage 和 message_id；密码验证成功后按到达顺序交回
FeishuAdapter 的唯一正常分发入口。队列只负责短暂接力，不拥有会话或任务语义。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import deque
from typing import Literal

from .protocol import IncomingMessage

logger = logging.getLogger(__name__)

DEFAULT_PER_USER_CAP = 20
DEFAULT_TOTAL_CAP = 10_000
DEFAULT_RECENT_ID_CAP = 10_000

EnqueueStatus = Literal["added", "duplicate", "full", "inactive"]
_MessageKey = tuple[str, str, str, str]


class FeishuUnlockQueue:
    """按用户 FIFO 暂存被闲置锁拦下的飞书消息，并按 message_id 去重。"""

    def __init__(
        self,
        *,
        per_user_cap: int = DEFAULT_PER_USER_CAP,
        total_cap: int = DEFAULT_TOTAL_CAP,
        recent_id_cap: int = DEFAULT_RECENT_ID_CAP,
    ) -> None:
        self._per_user_cap = max(1, int(per_user_cap))
        self._total_cap = max(self._per_user_cap, int(total_cap))
        self._recent_id_cap = max(1, int(recent_id_cap))
        self._lock = threading.Lock()
        self._queues: dict[str, deque[IncomingMessage]] = {}
        self._live_keys: set[_MessageKey] = set()
        self._draining: set[str] = set()
        self._recent_keys: set[_MessageKey] = set()
        self._recent_order: deque[_MessageKey] = deque()
        self._queued_count = 0

    def enqueue_locked(self, message: IncomingMessage) -> EnqueueStatus:
        """首次锁定或仍在等待解锁时入队。"""
        user_id = str(message.user_id or "")
        if not user_id:
            return "full"
        with self._lock:
            queue = self._queues.setdefault(user_id, deque())
            status = self._enqueue(queue, message)
            if status != "added" and not queue and user_id not in self._draining:
                self._queues.pop(user_id, None)
            return status

    def enqueue_if_pending(self, message: IncomingMessage) -> EnqueueStatus:
        """该用户已有待续送链时继续排队；没有则返回 inactive。"""
        user_id = str(message.user_id or "")
        if not user_id:
            return "inactive"
        with self._lock:
            queue = self._queues.get(user_id)
            if queue is None:
                return "inactive"
            return self._enqueue(queue, message)

    def was_resumed(self, message: IncomingMessage) -> bool:
        """识别平台对已经续送消息的迟到重投，避免再次执行。"""
        key = self._message_key(message)
        with self._lock:
            return key in self._recent_keys

    def begin_drain(self, user_id: str) -> bool:
        """原子取得该用户的 drain 所有权；重复解锁不会启动第二个消费者。"""
        with self._lock:
            if user_id in self._draining or user_id not in self._queues:
                return False
            self._draining.add(user_id)
            return True

    def cancel_drain(self, user_id: str) -> None:
        """线程未能启动时释放 drain 所有权，队列内容保留。"""
        with self._lock:
            self._draining.discard(user_id)

    def next_for_drain(self, user_id: str) -> IncomingMessage | None:
        """取下一条；队列耗尽时原子结束 drain，使后续消息恢复直接分发。"""
        with self._lock:
            if user_id not in self._draining:
                return None
            queue = self._queues.get(user_id)
            if not queue:
                self._queues.pop(user_id, None)
                self._draining.discard(user_id)
                return None
            self._queued_count -= 1
            return queue.popleft()

    def complete(self, message: IncomingMessage) -> None:
        """续送调用返回后登记 message_id 墓碑，并释放 live 身份。"""
        key = self._message_key(message)
        with self._lock:
            self._live_keys.discard(key)
            if key in self._recent_keys:
                return
            while len(self._recent_order) >= self._recent_id_cap:
                self._recent_keys.discard(self._recent_order.popleft())
            self._recent_order.append(key)
            self._recent_keys.add(key)

    def is_draining(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._draining

    def depth(self, user_id: str) -> int:
        with self._lock:
            return len(self._queues.get(user_id, ()))

    def _enqueue(
        self,
        queue: deque[IncomingMessage],
        message: IncomingMessage,
    ) -> EnqueueStatus:
        key = self._message_key(message)
        if key in self._live_keys or key in self._recent_keys:
            return "duplicate"
        if len(queue) >= self._per_user_cap or self._queued_count >= self._total_cap:
            return "full"
        queue.append(message)
        self._live_keys.add(key)
        self._queued_count += 1
        return "added"

    @staticmethod
    def _message_key(message: IncomingMessage) -> _MessageKey:
        message_id = str(message.message_id or "").strip()
        if not message_id:
            fallback = "\0".join(
                (
                    str(message.timestamp),
                    str(message.conversation_id or ""),
                    str(message.content or ""),
                )
            )
            message_id = "anonymous:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()
        return (
            str(message.channel or ""),
            str(message.user_id or ""),
            str(message.conversation_id or ""),
            message_id,
        )


class FeishuUnlockResumeMixin:
    """把解锁续送职责从 FeishuAdapter 主类拆开，仍调用其唯一 `_dispatch` 入口。"""

    _pending_unlock_messages: FeishuUnlockQueue | None

    def _queue_locked_message(self, msg: IncomingMessage) -> EnqueueStatus:
        queue = self._pending_unlock_messages
        return queue.enqueue_locked(msg) if queue is not None else "inactive"

    def _queue_if_unlock_pending(self, msg: IncomingMessage) -> EnqueueStatus:
        queue = self._pending_unlock_messages
        return queue.enqueue_if_pending(msg) if queue is not None else "inactive"

    def _report_unlock_queue_status(
        self,
        msg: IncomingMessage,
        status: EnqueueStatus,
    ) -> None:
        if status == "full":
            logger.error(
                "飞书解锁待续送队列已满，消息未入队(message_id=%s)",
                str(msg.message_id or ""),
            )

    def _resume_pending_unlock_messages(self, user_id: str) -> bool:
        """解锁成功后异步沿唯一正常分发入口续送原消息；重复卡片回调只启动一次。"""
        queue = self._pending_unlock_messages
        if queue is None or not queue.begin_drain(user_id):
            return False
        try:
            threading.Thread(
                target=self._drain_pending_unlock_messages,
                args=(user_id,),
                name="my-agent-feishu-unlock-resume",
                daemon=True,
            ).start()
        except Exception as exc:
            queue.cancel_drain(user_id)
            logger.error(
                "飞书解锁待续送线程启动失败: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False
        return True

    def _drain_pending_unlock_messages(self, user_id: str) -> None:
        queue = self._pending_unlock_messages
        if queue is None:
            return
        while (msg := queue.next_for_drain(user_id)) is not None:
            try:
                self._dispatch(msg)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.error(
                    "飞书解锁后续送消息失败(message_id=%s): %s: %s",
                    str(msg.message_id or ""),
                    type(exc).__name__,
                    exc,
                )
            finally:
                queue.complete(msg)


__all__ = [
    "DEFAULT_PER_USER_CAP",
    "DEFAULT_RECENT_ID_CAP",
    "DEFAULT_TOTAL_CAP",
    "EnqueueStatus",
    "FeishuUnlockQueue",
    "FeishuUnlockResumeMixin",
]
