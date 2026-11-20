"""后台显示端口共用的运行级发布决定。"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from types import MethodType
from typing import Literal
from weakref import WeakMethod, WeakValueDictionary

Port = Literal["activity", "archive", "history", "checkpoint", "final", "frozen_retry"]
Decision = Literal["pending", "verified", "withheld"]
WITHHELD_TEXT = "[内容因重启后凭据脱敏状态无法核验已安全扣留]"


@dataclass(frozen=True, slots=True)
class PublicationWrite:
    value: object
    emit: Callable[[object], object]
    withhold_record: bool = False


@cache
def _raw_publication_window() -> int:
    # 复用原文本投影窗口，不在发布端维护第二套大小配置。
    from ..gateway_parts.progress_text_delta import TextDeltaBuffer

    return TextDeltaBuffer().max_buffer_chars


class PublicationGate:
    """只在运行存活时持有待发布数据，终态决定只能签发一次。

    决定与排空进度分开记录：终态后队列头只在其写入成功后才出队，失败条目与其后的未应用条目
    留在本运行私有队列里，重试 resolve 沿原顺序续写（各端口依赖原有幂等键避免重复）。
    排空由独立锁串行，门锁不跨 I/O 持有；排空线程内的重入写入直接执行。
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._decision: Decision = "pending"
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._drainer: int | None = None
        self._held: deque[tuple[Port, PublicationWrite]] = deque()
        self._held_bytes = 0
        self._overflow = False

    @property
    def decision(self) -> Decision:
        with self._lock:
            return self._decision

    @property
    def settled(self) -> bool:
        """终态已签发且所有排队条目都已应用。"""
        with self._lock:
            return self._decision != "pending" and not self._held

    def write(self, port: Port, payload: object) -> None:
        self.publish(port, payload)

    def publish(self, port: Port, payload: object) -> object:
        """已核验端口同步返回原回执，待决端口只接收可有界复制的展示值。"""
        if not isinstance(payload, PublicationWrite):
            raise TypeError("display publication requires PublicationWrite")
        with self._lock:
            decision = self._decision
            if decision == "pending":
                self._hold(port, payload)
                return None
            # 仍有未应用的早先条目时，后来者排在其后，不能越过失败条目先落盘。
            queued = bool(self._held) and self._drainer != threading.get_ident()
            if queued:
                self._held.append((port, payload))
        if queued:
            self._drain()
            return None
        return self._emit(decision, payload)

    @staticmethod
    def _emit(decision: Decision, payload: PublicationWrite) -> object:
        if decision == "verified" or payload.withhold_record:
            return payload.emit(payload.value)
        return None

    def _hold(self, port: Port, payload: PublicationWrite) -> None:
        from .background_transcript import (
            BACKGROUND_TRANSCRIPT_MAX_BYTES,
            BACKGROUND_TRANSCRIPT_MAX_EVENTS,
            _copy_display_payload,
        )

        if self._overflow:
            return
        try:
            if len(self._held) >= BACKGROUND_TRANSCRIPT_MAX_EVENTS:
                raise ValueError("display publication queue is full")
            maximum = min(_raw_publication_window(), BACKGROUND_TRANSCRIPT_MAX_BYTES - self._held_bytes)
            value, size = _copy_display_payload({"value": payload.value}, maximum)
        except (ValueError, RuntimeError):
            self._overflow = True
            self._held.clear()
            self._held_bytes = 0
            return
        emit = payload.emit
        if isinstance(emit, MethodType):
            # 私有队列不能反向延长原接收器的寿命。
            receiver = WeakMethod(emit)

            def emit(value: object) -> object:
                method = receiver()
                return method(value) if method is not None else None

        self._held.append((port, PublicationWrite(value["value"], emit, payload.withhold_record)))
        self._held_bytes += size

    def pending_record_budget(self) -> int:
        from .background_transcript import BACKGROUND_TRANSCRIPT_MAX_BYTES

        with self._lock:
            return min(_raw_publication_window(), BACKGROUND_TRANSCRIPT_MAX_BYTES - self._held_bytes)

    def mark_overflow(self) -> None:
        with self._lock:
            if self._decision == "pending":
                self._overflow = True
                self._held.clear()
                self._held_bytes = 0

    def resolve(self, decision: Literal["verified", "withheld"]) -> None:
        """签发终态（仅首次生效）并排空队列；排空中途失败时原样抛出，可再次调用续写。"""
        if decision not in ("verified", "withheld"):
            raise ValueError("display publication requires a terminal decision")
        with self._lock:
            if self._decision == "pending":
                self._decision = "withheld" if self._overflow else decision
                self._held_bytes = 0
            if not self._held or self._drainer == threading.get_ident():
                return
        self._drain()

    def _drain(self) -> None:
        with self._drain_lock:
            with self._lock:
                self._drainer = threading.get_ident()
            try:
                while True:
                    with self._lock:
                        if not self._held:
                            return
                        decision = self._decision
                        head = self._held[0]
                    self._emit(decision, head[1])
                    with self._lock:
                        if self._held and self._held[0] is head:
                            self._held.popleft()
            finally:
                with self._lock:
                    self._drainer = None


_gates: WeakValueDictionary[str, PublicationGate] = WeakValueDictionary()
_gates_lock = threading.Lock()


def publication_gate_for(run_id: str) -> PublicationGate:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("display publication requires a run ID")
    with _gates_lock:
        gate = _gates.get(run_id)
        if gate is None:
            gate = PublicationGate(run_id)
            _gates[run_id] = gate
        return gate


def has_publication_provenance(request: object) -> bool:
    wake = getattr(request, "wake_signal", None)
    metadata = wake.get("metadata") if isinstance(wake, dict) else None
    return isinstance(metadata, dict) and (
        str(metadata.get("input_provider") or "").strip().lower() == "credential"
        or isinstance(metadata.get("credential_result"), dict)
        or bool(metadata.get("credential_ref"))
    )


def is_withheld_publication(metadata: object, *, verified: bool = False) -> bool:
    if not isinstance(metadata, dict):
        return False
    withheld = metadata.get("credential_delivery_withheld")
    return (
        metadata.get("publication_decision") == "withheld"
        or (isinstance(withheld, dict) and withheld.get("action") == "withheld")
        or (metadata.get("credential_delivery_scoped") is True
            and not verified and metadata.get("credential_redaction_verified") is not True)
    )


def attached_publication_sink(request: object):
    sink = getattr(request, "_background_publication_sink", None)
    run_id = str(getattr(request, "conversation_turn_id", "") or "")
    if sink is not None and sink.publication_gate.run_id == run_id:
        return sink
    return None
