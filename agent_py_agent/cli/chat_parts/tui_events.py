# LLM: 本模块定义 chat TUI 唯一显示事件信封和有序 journal；业务状态仍归 Conversation/ToolRuntime/Gateway 权威源，事件只做可恢复 UI 投影。
# 模块用途: 给本地 worker、Gateway、输入控制和界面 reducer 提供统一、幂等、可排序的 typed event。

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TUI_EVENT_VERSION = 1
TUI_EVENT_PHASES = frozenset(
    {
        "started",
        "delta",
        "waiting_permission",
        "completed",
        "failed",
        "interrupted",
        "queued",
        "removed",
        "restored",
        "updated",
    }
)


# LLM: TuiEvent 是 UI 跨线程/重放的最小稳定信封；kind 保持开放，phase 是生命周期协议，payload 不获得业务执行权。
# 类用途: 表示一个带稳定身份、来源序号和业务引用的界面事实。
@dataclass(frozen=True)
class TuiEvent:
    event_id: str
    seq: int
    stream_id: str
    block_id: str
    kind: str
    phase: str
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    request_id: str = ""
    turn_id: str = ""
    created_at: float = 0.0
    version: int = TUI_EVENT_VERSION

    # LLM: 校验只约束信封与有限生命周期，不封闭开放 kind/payload；错误必须在进入 journal 前暴露。
    # 函数用途: 规范字符串、复制 payload，并拒绝空身份、无效版本和倒退序号。
    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        stream_id = str(self.stream_id or "").strip()
        block_id = str(self.block_id or "").strip()
        kind = str(self.kind or "").strip()
        phase = str(self.phase or "").strip().lower()
        if self.version != TUI_EVENT_VERSION:
            raise ValueError(f"unsupported TUI event version: {self.version}")
        if not event_id or not stream_id or not block_id or not kind:
            raise ValueError("TUI event requires event_id/stream_id/block_id/kind")
        if int(self.seq) <= 0:
            raise ValueError("TUI event seq must be positive")
        if phase not in TUI_EVENT_PHASES:
            raise ValueError(f"invalid TUI event phase: {phase}")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(self, "block_id", block_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "payload", dict(self.payload or {}))
        object.__setattr__(self, "created_at", max(0.0, float(self.created_at or 0.0)))


# LLM: JournalAppendResult 明确区分首次接受、幂等重放和拒绝，调用方不得用异常文案猜测结果。
# 类用途: 返回 journal 对单条事件的结构化裁决。
@dataclass(frozen=True)
class JournalAppendResult:
    status: str
    reason: str = ""

    # LLM: accepted 只代表该事件应进入 reducer；duplicate/rejected 都不能再次改变 view model。
    # 函数用途: 判断本次 append 是否产生新的可应用事件。
    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


# LLM: TuiEventSequencer 为一个来源流生成单调 seq 与唯一 event id；它不负责 block 身份或持久化。
# 类用途: 让 worker/Gateway adapter 在并发线程中安全生成事件信封。
class TuiEventSequencer:
    # LLM: 构造时固定 stream/session/request 上下文，后续 emit 不能静默改写来源范围。
    # 函数用途: 创建从指定序号开始的线程安全事件生成器。
    def __init__(
        self,
        stream_id: str,
        *,
        session_id: str = "",
        request_id: str = "",
        turn_id: str = "",
        start_seq: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        normalized = str(stream_id or "").strip()
        if not normalized:
            raise ValueError("TUI sequencer requires stream_id")
        self.stream_id = normalized
        self.session_id = str(session_id or "")
        self.request_id = str(request_id or "")
        self.turn_id = str(turn_id or "")
        self._seq = max(0, int(start_seq or 0))
        self._clock = clock
        self._lock = threading.Lock()

    # LLM: emit 只分配 envelope 身份，kind/phase/payload 语义由 adapter 负责并由 reducer 验证转换。
    # 函数用途: 生成下一条单调 typed TUI event。
    def emit(
        self,
        kind: str,
        phase: str,
        block_id: str,
        payload: dict[str, Any] | None = None,
        *,
        event_id: str = "",
        session_id: str | None = None,
        request_id: str | None = None,
        turn_id: str | None = None,
    ) -> TuiEvent:
        with self._lock:
            self._seq += 1
            seq = self._seq
        stable_event_id = str(event_id or "").strip() or f"tui_{uuid.uuid4().hex}"
        return TuiEvent(
            event_id=stable_event_id,
            seq=seq,
            stream_id=self.stream_id,
            block_id=block_id,
            kind=kind,
            phase=phase,
            payload=dict(payload or {}),
            session_id=self.session_id if session_id is None else str(session_id or ""),
            request_id=self.request_id if request_id is None else str(request_id or ""),
            turn_id=self.turn_id if turn_id is None else str(turn_id or ""),
            created_at=self._clock(),
        )


# LLM: TuiEventJournal 是 UI 事件顺序与去重的唯一账；它不渲染，也不替代 canonical conversation/tool records。
# 类用途: 按 stream 检查单调序号、按 event_id 幂等，并保留有界重放窗口。
class TuiEventJournal:
    # LLM: UI 诊断事件不是历史数据库；限制条数、正文和流游标，当前流单调性与近期身份冲突仍检查。
    # 函数用途: 创建有界短期事件账，避免流式长聊保留数万份已经过期的文字。
    def __init__(self, *, max_events: int = 256, max_seen_ids: int = 2048,
                 max_payload_chars: int = 262_144, max_streams: int = 4096) -> None:
        self.max_events = max(1, int(max_events or 1))
        self.max_seen_ids = max(self.max_events, int(max_seen_ids or self.max_events))
        self.max_payload_chars = max(1, int(max_payload_chars))
        self.max_streams = max(1, int(max_streams))
        self._events: deque[TuiEvent] = deque(maxlen=self.max_events)
        self._seen: dict[str, TuiEvent] = {}
        self._seen_order: deque[tuple[str, int]] = deque()
        self._payload_chars = 0
        self._last_seq: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    # LLM: 近期同 ID 比较原事件，当前流检查单调性；正文预算只控制诊断保留，不阻止事件进入 reducer。
    # 函数用途: 幂等接收事件并裁剪旧诊断，完整历史仍从 canonical 分页读取。
    def append(self, event: TuiEvent) -> JournalAppendResult:
        with self._lock:
            previous = self._seen.get(event.event_id)
            if previous is not None:
                if previous == event:
                    return JournalAppendResult("duplicate", "event_id_replay")
                return JournalAppendResult("rejected", "event_id_conflict")
            last_seq = self._last_seq.get(event.stream_id, 0)
            if event.seq <= last_seq:
                return JournalAppendResult("rejected", "out_of_order_seq")
            self._events.append(event)
            self._seen[event.event_id] = event
            weight = _payload_text_size(event.payload)
            self._seen_order.append((event.event_id, weight))
            self._payload_chars += weight
            self._last_seq[event.stream_id] = event.seq
            self._last_seq.move_to_end(event.stream_id)
            self._trim_locked()
            return JournalAppendResult("accepted")

    # LLM: snapshot 返回不可变顺序副本，调用方不能取得内部容器后绕过 journal。
    # 函数用途: 读取当前有界事件窗口。
    def snapshot(self) -> tuple[TuiEvent, ...]:
        with self._lock:
            return tuple(self._events)

    # LLM: cursor 是每来源最后接受序号的只读投影，用于重连请求，不允许调用方回写。
    # 函数用途: 返回所有来源流的重放游标。
    def cursors(self) -> dict[str, int]:
        with self._lock:
            return dict(self._last_seq)

    # LLM: 只淘汰短期 UI 投影，不能删除磁盘历史；保留流按 LRU 有界，活跃流游标不会随正文裁剪消失。
    # 函数用途: 同步裁剪正文、去重身份和闲置来源游标，按 deque 从头释放而非搬移整个列表。
    def _trim_locked(self) -> None:
        while self._seen_order and (len(self._seen_order) > self.max_seen_ids
                                   or self._payload_chars > self.max_payload_chars):
            event_id, weight = self._seen_order.popleft()
            self._seen.pop(event_id, None)
            self._payload_chars -= weight
        while self._events and self._events[0].event_id not in self._seen:
            self._events.popleft()
        while len(self._last_seq) > self.max_streams:
            self._last_seq.popitem(last=False)


# LLM: 只估算 UI payload 中已有字符串，不序列化未知对象、不运行 __str__，不用于机器授权或状态判断。
# 函数用途: 给诊断保留窗口计算轻量字符预算。
def _payload_text_size(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_payload_text_size(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return sum(_payload_text_size(item) for item in value)
    return 0


__all__ = [
    "JournalAppendResult",
    "TUI_EVENT_PHASES",
    "TUI_EVENT_VERSION",
    "TuiEvent",
    "TuiEventJournal",
    "TuiEventSequencer",
]
