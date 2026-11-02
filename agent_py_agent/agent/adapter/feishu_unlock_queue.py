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

# 每个用户锁定期待送消息最多 20 条：防止单个用户刷爆队列，超出直接拒收。
DEFAULT_PER_USER_CAP_COUNT = 20
# 全队列总条目上限 1 万：锁定期极长时防内存被待送消息撑爆。
DEFAULT_TOTAL_CAP_COUNT = 10_000
# 最近去重 ID 保留 1 万个：防止去重集合无限增长。
DEFAULT_RECENT_ID_CAP_COUNT = 10_000
# 解锁续送被下游拒收或报错后的有界退避：首等 0.5 秒、逐次翻倍、最长 30 秒，本代停止事件随时打断。
UNLOCK_RESUME_RETRY_INITIAL_SECONDS = 0.5
UNLOCK_RESUME_RETRY_MAX_SECONDS = 30.0

EnqueueStatus = Literal["added", "duplicate", "full", "inactive"]
_MessageKey = tuple[str, str, str, str]


class FeishuUnlockQueue:
    """按用户 FIFO 暂存被闲置锁拦下的飞书消息，并按 message_id 去重。"""

    def __init__(
        self,
        *,
        per_user_cap: int = DEFAULT_PER_USER_CAP_COUNT,
        total_cap: int = DEFAULT_TOTAL_CAP_COUNT,
        recent_id_cap: int = DEFAULT_RECENT_ID_CAP_COUNT,
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
        """返回准确队首但不移出：下游受理前队首及其容量责任仍留在队列；耗尽时原子结束 drain。"""
        with self._lock:
            if user_id not in self._draining:
                return None
            queue = self._queues.get(user_id)
            if not queue:
                self._queues.pop(user_id, None)
                self._draining.discard(user_id)
                return None
            return queue[0]

    def complete(self, message: IncomingMessage) -> None:
        """下游受理后移出准确队首，登记 message_id 墓碑并释放 live 身份。"""
        key = self._message_key(message)
        with self._lock:
            queue = self._queues.get(str(message.user_id or ""))
            if queue and queue[0] is message:
                queue.popleft()
                self._queued_count -= 1
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

    # LLM: 生命周期锁内先核本代未停止、取得 drain 所有权并登记真实线程及代次，再启动；已停止的代次不起新续送。
    # 函数用途: 解锁成功后异步沿唯一正常分发入口续送原消息；重复卡片回调只启动一次。
    def _resume_pending_unlock_messages(self, user_id: str) -> bool:
        queue = self._pending_unlock_messages
        if queue is None:
            return False
        with self._lock:  # type: ignore[attr-defined]
            stopped = self._unlock_drain_stop  # type: ignore[attr-defined]
            if stopped.is_set() or not queue.begin_drain(user_id):
                return False
            drains = self._unlock_drain_threads  # type: ignore[attr-defined]
            for finished in [thread for thread in drains if thread.ident is not None and not thread.is_alive()]:
                drains.pop(finished, None)
            thread = threading.Thread(
                target=self._drain_pending_unlock_messages,
                args=(user_id, stopped),
                name="my-agent-feishu-unlock-resume",
                daemon=True,
            )
            drains[thread] = self._generation_ref  # type: ignore[attr-defined]
            try:
                thread.start()
            except Exception as exc:
                drains.pop(thread, None)
                queue.cancel_drain(user_id)
                logger.error(
                    "飞书解锁待续送线程启动失败: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                return False
        return True

    # LLM: 只在下游受理后移出准确队首；拒收/异常保留队首与容量，按有界退避在本代停止事件上可中断地重试。
    #   本代停止时释放 drain 所有权后退出，队首留给新代下次解锁续送；新代换新事件，旧线程不会因清除而复活。
    # 函数用途: 按 FIFO 续送一个用户的暂存消息。
    def _drain_pending_unlock_messages(self, user_id: str, stopped: threading.Event | None = None) -> None:
        queue = self._pending_unlock_messages
        if queue is None:
            return
        stopped = self._unlock_drain_stop if stopped is None else stopped  # type: ignore[attr-defined]
        delay = UNLOCK_RESUME_RETRY_INITIAL_SECONDS
        try:
            while (msg := queue.next_for_drain(user_id)) is not None:
                if stopped.is_set():
                    queue.cancel_drain(user_id)
                    return
                if self._hand_off_unlock_message(msg):
                    queue.complete(msg)
                    delay = UNLOCK_RESUME_RETRY_INITIAL_SECONDS
                elif stopped.wait(delay):
                    queue.cancel_drain(user_id)
                    return
                else:
                    delay = min(UNLOCK_RESUME_RETRY_MAX_SECONDS, delay * 2)
        except BaseException:
            # 仍持 drain 所有权时才会到这里：释放后让下次解锁可接手原队首。
            queue.cancel_drain(user_id)
            raise

    # LLM: 受理以原唯一分发入口返回为准：legacy None 及非 False 返回视为受理；显式 False 或异常不受理，不吞 BaseException。
    # 函数用途: 把一条暂存消息交给原正常分发入口，返回下游是否受理。
    def _hand_off_unlock_message(self, msg: IncomingMessage) -> bool:
        try:
            accepted = self._dispatch(msg) is not False  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(
                "飞书解锁后续送消息失败，保留队首重试(message_id=%s): %s: %s",
                str(msg.message_id or ""),
                type(exc).__name__,
                exc,
            )
            return False
        if not accepted:
            logger.warning("飞书解锁后续送未被受理，保留队首重试(message_id=%s)", str(msg.message_id or ""))
        return accepted


__all__ = [
    "DEFAULT_PER_USER_CAP_COUNT",
    "DEFAULT_RECENT_ID_CAP_COUNT",
    "DEFAULT_TOTAL_CAP_COUNT",
    "EnqueueStatus",
    "FeishuUnlockQueue",
    "FeishuUnlockResumeMixin",
]
