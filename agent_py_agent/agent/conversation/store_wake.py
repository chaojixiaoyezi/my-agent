# LLM: Wake-signal queue dedupes urgent work without running the agent inline.
# 模块用途: 创建、读取、去重和处理后台主代理唤醒信号。

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..gateway_parts.io import read_json_file, update_json_file_atomic, write_json_file_atomic
from .models import ObservationEvent, WakeSignal, new_id
from .store_common import now as current_time
from .store_common import wake_evidence_refs, wake_urgency
from .store_guidance import ConversationGuidanceStore


class ConversationWakeStore(ConversationGuidanceStore):
    def raise_wake_signal(self, request: dict) -> WakeSignal:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        kwargs = {
            "observation": request.get("observation"),
            "urgency": request.get("urgency", "urgent"),
            "severity": request.get("severity", ""),
            "reason": request.get("reason", "agent_event"),
            "source_agent_id": request.get("source_agent_id", ""),
            "parent_agent_id": request.get("parent_agent_id", ""),
            "root_task_id": request.get("root_task_id", ""),
            "summary": request.get("summary", ""),
            "evidence_refs": request.get("evidence_refs"),
            "dedupe_key": request.get("dedupe_key", ""),
            "metadata": request.get("metadata") or {},
        }
        signal = _wake_signal(thread.thread_id, current_time(request.get("now")), kwargs)
        if signal.dedupe_key:
            return self._raise_deduped_wake_signal(signal)
        write_json_file_atomic(self._wake_signal_path(signal), signal.to_dict())
        return signal

    def pending_wake_signals(self, *, limit: int = 100, include_normal: bool = True) -> list[WakeSignal]:
        signals = [signal for kind in _wake_kinds(include_normal) for signal in self._pending_signals(kind)]
        signals.sort(key=lambda item: (0 if item.urgency == "urgent" else 1, item.created_at))
        return signals if limit <= 0 else signals[:limit]

    def mark_wake_signal_handled(self, wake_signal_id: str, *, now: float | None = None) -> WakeSignal | None:
        path = self._find_wake_signal_path(wake_signal_id)
        if path is None or not (data := read_json_file(path)):
            return None
        handled = replace(WakeSignal.from_dict(data), status="handled", handled_at=now if now is not None else __import__("time").time())
        write_json_file_atomic(self.wake_handled_dir / f"{handled.wake_signal_id}.json", handled.to_dict())
        _unlink_quietly(path)
        if handled.observation_id:
            self.mark_observations_handled([handled.observation_id], now=handled.handled_at)
        return handled

    def _find_wake_signal_path(self, wake_signal_id: str):
        name = f"{wake_signal_id}.json"
        return next((path for kind in ("urgent", "normal") if (path := self.wake_queue_dir / kind / name).exists()), None)

    def _pending_signals(self, kind: str) -> list[WakeSignal]:
        signals = [WakeSignal.from_dict(data) for path in sorted((self.wake_queue_dir / kind).glob("*.json")) if (data := read_json_file(path))]
        return [signal for signal in signals if signal.status == "pending"]

    def _raise_deduped_wake_signal(self, signal: WakeSignal) -> WakeSignal:
        selected: WakeSignal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal selected
            existing = self._pending_wake_by_id(str(data.get("wake_signal_id") or ""))
            if existing is not None and existing.thread_id == signal.thread_id:
                selected = existing
                return data
            write_json_file_atomic(self._wake_signal_path(signal), signal.to_dict())
            selected = signal
            return {"schema_version": "wake_dedupe.v1", "thread_id": signal.thread_id, "dedupe_key": signal.dedupe_key, "wake_signal_id": signal.wake_signal_id, "updated_at": signal.created_at}

        update_json_file_atomic(self._wake_dedupe_path(signal.thread_id, signal.dedupe_key), updater)
        return selected or signal

    def _pending_wake_by_id(self, wake_signal_id: str) -> WakeSignal | None:
        path = self._find_wake_signal_path(wake_signal_id)
        data = read_json_file(path) if path is not None else {}
        signal = WakeSignal.from_dict(data) if data else None
        return signal if signal is not None and signal.status == "pending" else None


def _wake_signal(thread_id: str, current: float, kwargs: dict[str, Any]) -> WakeSignal:
    observation = kwargs.get("observation")
    observation = observation if isinstance(observation, ObservationEvent) else None
    return WakeSignal(
        wake_signal_id=new_id("wake"),
        thread_id=thread_id,
        observation_id=observation.observation_id if observation is not None else "",
        urgency=wake_urgency(kwargs.get("urgency", "urgent")),
        severity=kwargs.get("severity") or (observation.severity if observation is not None else ""),
        reason=str(kwargs.get("reason") or "agent_event"),
        source_agent_id=kwargs.get("source_agent_id") or (observation.source_agent_id if observation is not None else ""),
        parent_agent_id=kwargs.get("parent_agent_id") or (observation.parent_agent_id if observation is not None else ""),
        root_task_id=kwargs.get("root_task_id") or (observation.root_task_id if observation is not None else ""),
        summary=kwargs.get("summary") or (observation.summary if observation is not None else ""),
        evidence_refs=wake_evidence_refs(observation, kwargs.get("evidence_refs")),
        created_at=current,
        dedupe_key=str(kwargs.get("dedupe_key") or ""),
        metadata=kwargs.get("metadata") or {},
    )


def _wake_kinds(include_normal: bool) -> tuple[str, ...]:
    return ("urgent", "normal") if include_normal else ("urgent",)


def _unlink_quietly(path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
