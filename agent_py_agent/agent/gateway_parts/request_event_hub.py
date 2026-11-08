"""Bounded, metadata-only request notifications; no request-content I/O."""

from __future__ import annotations

import importlib
import math
import os
import re
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType


# LLM: Normalize numeric configuration without propagating conversion errors.
# 函数用途: 将配置转换为整数，转换失败时使用默认值。
def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


# LLM: Return finite, nonnegative seconds without introducing a timing policy.
# 函数用途: 清理等待时间，拒绝负数、无穷值和无效输入。
def _seconds(value: object) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0.0
    except Exception:
        return 0.0


# LLM: Accept exact strings containing tokens or explicitly versioned cursors.
# 函数用途: 只允许安全标识符，以及游标字段使用的版本化游标格式。
def _safe_string(value: object, *, cursor: bool = False) -> bool:
    if type(value) is not str:
        return False
    pattern = r"[A-Za-z0-9_-]+"
    if cursor:
        pattern += r"|v1(?:[.:][A-Za-z0-9_-]+)+"
    return re.fullmatch(pattern, value) is not None


# LLM: Copy only allowlisted scalar metadata; invoke input mapping code here.
# 函数用途: 丢弃正文、路径和未知字段，只保留允许的标量元数据。
def _safe_ref(ref: object) -> dict[str, object]:
    result: dict[str, object] = {}
    if not isinstance(ref, Mapping):
        return result
    for key in ("cursor", "offset", "version", "attempt_id", "kind"):
        try:
            value = ref.get(key)
        except Exception:
            continue
        if (
            type(value) in (bool, int)
            or (type(value) is float and math.isfinite(value))
            or _safe_string(value, cursor=key == "cursor")
        ):
            result[key] = value
    return result


# LLM: Canonicalize topics before locking; never retain caller-defined objects.
# 函数用途: 将主题转换为仅包含安全字符串的不可变元组。
def _topic_key(topic: object) -> tuple[str, ...] | None:
    parts = (topic,) if type(topic) is str else topic
    if type(parts) is not tuple or not parts:
        return None
    return parts if all(type(part) is str and part and part.isprintable() for part in parts) else None


# LLM: Lazily reuse worker polling, with configured/interpreter fallbacks.
# 函数用途: 为停止信号选择轮询间隔，不新增业务时间常量。
def _wait_quantum(fallback: float) -> float:
    try:
        name = f"{__package__}.request_worker" if __package__ else "request_worker"
        worker = importlib.import_module(name)
        quantum = _seconds(worker._WAIT_POLL_STEPS_SECONDS[0])
        if quantum > 0:
            return quantum
    except Exception:
        pass
    return fallback if fallback > 0 else sys.getswitchinterval()


@dataclass(frozen=True, slots=True)
class RequestEvent:
    seq: int
    kind: str
    request_id: str
    ref: Mapping[str, object]

    # LLM: Freeze a sanitized copy; retain no reference to caller-owned mappings.
    # 函数用途: 为通知创建只读元数据副本，避免外部修改通知内容。
    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", MappingProxyType(_safe_ref(self.ref)))


@dataclass(frozen=True, slots=True)
class Gap:
    seq: int = 0
    reset_required: bool = True
    reason: str = "reset"


@dataclass(slots=True)
class _Topic:
    ring: deque[RequestEvent]
    touched: float
    last_seq: int = 0
    dropped_through: int = 0
    subscribers: weakref.WeakSet[Subscription] = field(default_factory=weakref.WeakSet)


class Subscription:
    # LLM: Allocate an initially detached subscription; the hub attaches it.
    # 函数用途: 创建订阅状态和唤醒事件，随后由事件中心完成绑定。
    def __init__(self, hub: RequestEventHub) -> None:
        self._hub = hub
        self._state: _Topic | None = None
        self._cursor = 0
        self._gap: Gap | None = None
        self._closed = True
        self._event = threading.Event()

    # LLM: Expose the integration event; read() owns acknowledgement/clearing.
    # 函数用途: 提供传输层等待用的事件，调用方不应自行清除该事件。
    @property
    def event(self) -> threading.Event:
        return self._event

    # LLM: Atomically drain metadata or acknowledge a reset; never perform I/O.
    # 函数用途: 读取尚未消费的通知，历史缺失时返回需要重置的标记。
    def read(self) -> tuple[RequestEvent | Gap, ...]:
        now = time.monotonic()
        with self._hub._lock:
            state = self._state
            if self._gap is not None:
                gap, self._gap = self._gap, None
                if state is not None:
                    self._cursor = state.last_seq
                    state.touched = now
                    gap = Gap(seq=state.last_seq, reason=gap.reason)
                self._event.clear()
                return (gap,)
            if self._closed or state is None:
                return ()
            state.touched = now
            if self._cursor < state.dropped_through:
                result = (Gap(seq=state.last_seq, reason="overflow"),)
            else:
                result = tuple(event for event in state.ring
                               if event.seq > self._cursor)
            self._cursor = state.last_seq
            self._event.clear()
            return result

    # LLM: Wait outside hub locks; stop checks and lazy imports are also unlocked.
    # 函数用途: 等待通知或超时，并在收到停止信号时及时退出。
    def wait(self, timeout: float | None, stop=None) -> bool:
        deadline = None if timeout is None else time.monotonic() + _seconds(timeout)
        quantum = _wait_quantum(self._hub.fallback_poll_seconds) if stop is not None else None
        while True:
            if stop is not None and stop.is_set():
                return False
            with self._hub._lock:
                if self._closed and self._gap is None:
                    return False
                if self._event.is_set():
                    return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            interval = remaining
            if quantum is not None:
                interval = quantum if remaining is None else min(quantum, remaining)
            self._event.wait(interval)

    # LLM: Detach idempotently and wake waiters without invoking consumers.
    # 函数用途: 关闭订阅、释放主题名额，并唤醒正在等待的线程。
    def close(self) -> None:
        now = time.monotonic()
        with self._hub._lock:
            if self._state is not None:
                self._state.subscribers.discard(self)
                self._state.touched = now
            self._state = None
            self._gap = None
            self._closed = True
            self._event.set()


class SignalView:
    # LLM: Retain one persistent subscription rather than subscribe per wait.
    # 函数用途: 持有长期订阅，为传输层提供合并后的唤醒信号。
    def __init__(self, subscription: Subscription) -> None:
        self._subscription = subscription

    # LLM: Consume a successful wake atomically through the subscription.
    # 函数用途: 等待并消费一次唤醒，避免历史通知导致持续空转。
    def wait(self, timeout: float | None, stop=None) -> bool:
        if self._subscription._closed and self._subscription._gap is None:
            (stop if stop is not None else threading.Event()).wait(timeout)
            return False
        if not self._subscription.wait(timeout, stop):
            return False
        self._subscription.read()
        return True

    # LLM: Wake transport waiters without publishing or calling consumer code.
    # 函数用途: 主动唤醒传输线程，供停止流程使用。
    def wake(self) -> None:
        subscription = self._subscription
        with subscription._hub._lock:
            subscription._event.set()

    # LLM: Release the underlying subscription idempotently.
    # 函数用途: 关闭信号视图并释放其订阅名额。
    def close(self) -> None:
        self._subscription.close()


class RequestEventHub:
    # LLM: Configure bounded in-memory storage; start no background threads.
    # 函数用途: 初始化有界事件中心，不创建后台线程或读取请求文件。
    def __init__(self, *, ring_events: int, topic_capacity: int,
                 subscribers_per_topic: int, idle_seconds: float,
                 fallback_poll_seconds: float = 0) -> None:
        self.ring_events = max(1, _integer(ring_events))
        self.topic_capacity = max(1, _integer(topic_capacity))
        self.subscribers_per_topic = max(1, _integer(subscribers_per_topic))
        self.idle_seconds = _seconds(idle_seconds)
        self.fallback_poll_seconds = _seconds(fallback_poll_seconds)
        self._lock = threading.Lock()
        self._topics: dict[tuple[str, ...], _Topic] = {}
        self._owner_hints: dict[str, str] = {}
        self._closed = False

    # LLM: Under the hub lock, reuse a topic or evict only an idle unsubscribed one.
    # 函数用途: 获取主题，在容量不足时仅淘汰无订阅且已空闲的主题。
    def _topic_locked(self, key: tuple[str, ...], now: float) -> _Topic | None:
        state = self._topics.get(key)
        if state is not None:
            return state
        if len(self._topics) >= self.topic_capacity:
            expired = next((name for name, item in self._topics.items()
                            if not item.subscribers
                            and now - item.touched >= self.idle_seconds), None)
            if expired is None:
                return None
            del self._topics[expired]
        state = _Topic(deque(maxlen=self.ring_events), now)
        self._topics[key] = state
        return state

    # LLM: Sanitize before locking; append metadata and signal without callbacks/I/O.
    # 函数用途: 非阻塞地发布安全元数据；关闭、容量不足或输入无效时返回零。
    def publish(self, topic, kind, request_id, ref) -> int:
        try:
            key = _topic_key(topic)
            if key is None or not _safe_string(kind):
                return 0
            if request_id != "" and not _safe_string(request_id):
                return 0
            if type(request_id) is not str:
                return 0
            metadata = _safe_ref(ref)
            now = time.monotonic()
            with self._lock:
                if self._closed:
                    return 0
                state = self._topic_locked(key, now)
                if state is None:
                    return 0
                sequence = state.last_seq + 1
                event = RequestEvent(sequence, kind, request_id, metadata)
                if len(state.ring) == self.ring_events:
                    state.dropped_through = state.ring[0].seq
                state.ring.append(event)
                state.last_seq = sequence
                state.touched = now
                for subscription in state.subscribers:
                    subscription._event.set()
                return sequence
        except Exception:
            return 0

    # LLM: Attach a bounded subscription or return an immediately readable Gap.
    # 函数用途: 建立订阅并检查历史游标；拒绝订阅时也返回明确的重置标记。
    def subscribe(self, topic, after_seq: int | None = None, *, wake_event=None) -> Subscription:
        key = _topic_key(topic)
        valid_cursor = after_seq is None or (type(after_seq) is int and after_seq >= 0)
        subscription = Subscription(self)
        if type(wake_event) is threading.Event:
            subscription._event = wake_event
        now = time.monotonic()
        with self._lock:
            reason = "closed" if self._closed else ""
            if not reason and (key is None or not valid_cursor):
                reason = "invalid"
            fresh = key not in self._topics
            state = None if reason else self._topic_locked(key, now)
            if not reason and state is None:
                reason = "topic_capacity"
            if state is not None and len(state.subscribers) >= self.subscribers_per_topic:
                reason = "subscriber_capacity"
            if reason:
                subscription._gap = Gap(reason=reason)
                subscription._event.set()
                return subscription
            subscription._state = state
            subscription._closed = False
            subscription._cursor = state.last_seq if after_seq is None else after_seq
            cursor = subscription._cursor
            if cursor and (
                fresh or cursor > state.last_seq
            ):
                subscription._gap = Gap(seq=state.last_seq, reason="restarted")
            elif cursor < state.dropped_through:
                subscription._gap = Gap(seq=state.last_seq, reason="overflow")
            if subscription._gap is not None or cursor < state.last_seq:
                subscription._event.set()
            state.subscribers.add(subscription)
            state.touched = now
            return subscription

    # LLM: Read-only: never creates a topic or subscription; the topic object identity detects eviction/recreation.
    # 函数用途: 返回 (主题对象, 最后序号)，供调用方 O(1) 判断“自上次读取后有无新提交事件”；主题不存在时为 (None, 0)。
    def topic_mark(self, topic) -> tuple[object | None, int]:
        key = _topic_key(topic)
        with self._lock:
            state = self._topics.get(key) if key is not None else None
            return state, (state.last_seq if state is not None else 0)

    # LLM: Return a persistent, independently closeable subscription-backed view.
    # 函数用途: 为传输层创建可长期等待并主动唤醒的信号视图。
    def signal(self, topic) -> SignalView:
        return SignalView(self.subscribe(topic))

    # LLM: Close all subscriptions and release rings without callbacks or I/O.
    # 函数用途: 关闭事件中心并唤醒全部订阅，后续发布直接返回零。
    def close(self) -> None:
        with self._lock:
            self._closed = True
            for state in self._topics.values():
                for subscription in state.subscribers:
                    subscription._closed = True
                    subscription._state = None
                    subscription._gap = None
                    subscription._event.set()
                state.subscribers.clear()
            self._topics.clear()
            self._owner_hints.clear()


# LLM: Build only when enabled; derive capacities exclusively from existing config.
# 函数用途: 根据已有配置创建事件中心，未启用环形缓冲时返回空值。
def hub_from_config(config) -> RequestEventHub | None:
    ring = _integer(getattr(config, "gateway_event_hub_ring_events", 0))
    if ring == 0:
        return None
    return RequestEventHub(
        ring_events=ring,
        topic_capacity=(
            _integer(getattr(config, "gateway_global_inflight_limit", 0))
            + 3 * _integer(getattr(config, "owner_agent_pool_max_agents", 0))
        ),
        subscribers_per_topic=_integer(getattr(config, "gateway_user_inflight_limit", 0)),
        idle_seconds=getattr(config, "owner_agent_idle_seconds", 0),
        fallback_poll_seconds=getattr(config, "gateway_event_fallback_poll_seconds", 0),
    )


_registry: weakref.WeakValueDictionary[str, RequestEventHub] = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()


# LLM: Compute lexical absolute keys outside registry locks; never resolve/stat.
# 函数用途: 从网关路径对象、根目录或分块文件路径推导词法绝对根目录。
def _root_key(paths_or_path) -> str | None:
    try:
        value = paths_or_path
        if not isinstance(value, (str, bytes, os.PathLike)):
            value = value.root
        path = Path(os.path.abspath(os.fspath(value)))
        if (
            path.name.endswith(".chunks.jsonl")
            and path.parent.name in ("processing", "done", "failed")
            and path.parent.parent.name == "requests"
        ):
            path = path.parent.parent.parent
        return str(path)
    except Exception:
        return None


# LLM: Register a weak hub reference after all path normalization is complete.
# 函数用途: 将网关根目录绑定到事件中心，不延长事件中心的存活时间。
def bind_hub(paths, hub: RequestEventHub) -> None:
    key = _root_key(paths)
    if key is not None:
        with _registry_lock:
            _registry[key] = hub


# LLM: Look up a hub using lexical keys without filesystem inspection.
# 函数用途: 按网关路径或分块路径查询已绑定的事件中心。
def hub_for_paths(paths_or_path) -> RequestEventHub | None:
    key = _root_key(paths_or_path)
    if key is None:
        return None
    with _registry_lock:
        return _registry.get(key)


# LLM: Remove only the specified binding, preserving any replacement hub.
# 函数用途: 仅解除仍指向指定事件中心的绑定，避免删除后续替换的实例。
def unbind_hub(paths, hub: RequestEventHub) -> None:
    key = _root_key(paths)
    if key is not None:
        with _registry_lock:
            if _registry.get(key) is hub:
                del _registry[key]


# LLM: Publish post-write hints only; never inspect request contents.
# 函数用途: 请求写入成功后，通知该请求的订阅者和调度订阅者。
def notify_request_written(paths, request_path) -> None:
    hub = hub_for_paths(paths)
    if hub is None:
        return
    try:
        request_id = Path(request_path).stem
    except Exception:
        return
    hub.publish(("request", request_id), "written", request_id, {})
    hub.publish(("dispatch",), "written", "", {})


# LLM: Only trusted post-commit callers pass frozen payloads; owner hints are bounded, non-authoritative and never expose paths/content in events.
# 函数用途: 持久提交后按请求和冻结owner唤醒读者；缺失提示仍由磁盘兜底，不决定完成或投递。
def notify_committed(paths, request_id: str, kind: str, *, payload=None, ref=None) -> None:
    hub = hub_for_paths(paths)
    if hub is None:
        return
    try:
        owner_id = ""
        if isinstance(payload, dict):
            from .request_owner_binding import frozen_request_owner
            if frozen_request_owner(payload) is not None:
                owner_id = str(payload.get("owner_id") or "")
        with hub._lock:
            if owner_id:
                if request_id not in hub._owner_hints and len(hub._owner_hints) >= hub.topic_capacity:
                    del hub._owner_hints[next(iter(hub._owner_hints))]
                hub._owner_hints[request_id] = owner_id
            owner_id = hub._owner_hints.get(request_id, "")
        hub.publish(("request", request_id), kind, request_id, ref or {})
        if owner_id:
            hub.publish(("owner", owner_id), kind, request_id, ref or {})
        if kind in {"written", "claimed", "terminal", "released"}:
            hub.publish(("dispatch",), kind, request_id, {})
    except Exception:
        # 唤醒是可丢的提示，异常绝不能回滚已提交事实。
        return


# LLM: Publish chunk metadata using lexical path information only.
# 函数用途: 分块写入后发布偏移量和尝试标识，不打开或读取分块文件。
def notify_chunk_written(chunk_path, *, offset: int, attempt_id: str = "") -> None:
    hub = hub_for_paths(chunk_path)
    if hub is None:
        return
    try:
        name = Path(chunk_path).name
        if not name.endswith(".chunks.jsonl"):
            return
        request_id = name.removesuffix(".chunks.jsonl")
    except Exception:
        return
    notify_committed(chunk_path, request_id, "chunk",
                     ref={"offset": offset, "attempt_id": attempt_id})
