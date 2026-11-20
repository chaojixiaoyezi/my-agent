"""后台显示端口共用的运行级发布决定。"""

from __future__ import annotations

import threading
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
    """只在运行存活时持有待发布数据，终态决定只能签发一次。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._decision: Decision = "pending"
        self._lock = threading.Lock()
        self._held: list[tuple[Port, PublicationWrite]] = []
        self._held_bytes = 0
        self._overflow = False

    @property
    def decision(self) -> Decision:
        with self._lock:
            return self._decision

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
        if decision not in ("verified", "withheld"):
            raise ValueError("display publication requires a terminal decision")
        with self._lock:
            if self._decision != "pending":
                return
            self._decision = "withheld" if self._overflow else decision
            held, self._held = self._held, []
            self._held_bytes = 0
        for port, payload in held:
            self.publish(port, payload)


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
