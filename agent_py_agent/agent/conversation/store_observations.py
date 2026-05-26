# LLM: Observation events record agent-tree facts without waking the LLM by default.
# 模块用途: 追加、查询和标记长期会话 observation。

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..gateway_parts.io import read_json_file, update_json_file_atomic
from ..io.jsonl import append_jsonl
from .models import ObservationEvent, new_id
from .store_common import now as current_time
from .store_common import read_jsonl, with_handled_at
from .store_tasks import ConversationTaskStore


@dataclass(frozen=True)
class _ObservationEventInput:
    thread_id: str
    event_type: str
    summary: str
    current: float
    kwargs: dict[str, Any]


class ConversationObservationStore(ConversationTaskStore):
    def append_observation(self, request: dict) -> ObservationEvent:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        kwargs = {
            "urgency": request.get("urgency", "normal"),
            "severity": request.get("severity", ""),
            "source_agent_id": request.get("source_agent_id", ""),
            "parent_agent_id": request.get("parent_agent_id", ""),
            "root_task_id": request.get("root_task_id", ""),
            "evidence_refs": request.get("evidence_refs") or [],
            "requires_main_agent": request.get("requires_main_agent", False),
            "requires_llm_report": request.get("requires_llm_report", False),
            "metadata": request.get("metadata") or {},
        }
        current = current_time(request.get("now"))
        event = _observation_event(_ObservationEventInput(thread.thread_id, str(request.get("event_type") or ""), str(request.get("summary") or ""), current, kwargs))
        append_jsonl(self._observation_path(thread_id), event.to_dict(), sort_keys=True)
        self._write_thread(replace(thread, updated_at=current))
        return event

    def recent_observations(self, thread_id: str, *, limit: int = 20, include_handled: bool = True) -> list[ObservationEvent]:
        handled = self._read_observation_handled()
        events = [with_handled_at(ObservationEvent.from_dict(row), handled) for row in read_jsonl(self._observation_path(thread_id))]
        if not include_handled:
            events = [event for event in events if event.handled_at <= 0]
        return events if limit <= 0 else events[-limit:]

    def unhandled_observations_requiring_main(self, *, limit: int = 20) -> list[ObservationEvent]:
        events = [event for path in sorted(self.observations_dir.glob("*.jsonl")) for event in self.recent_observations(path.stem, limit=0, include_handled=False) if event.requires_main_agent or event.requires_llm_report]
        events.sort(key=lambda item: item.observed_at)
        return events if limit <= 0 else events[:limit]

    def mark_observations_handled(self, observation_ids: list[str] | tuple[str, ...], *, now: float | None = None) -> None:
        ids = [str(item) for item in observation_ids if str(item or "").strip()]
        if ids:
            update_json_file_atomic(self.observation_handled_path, lambda data: {**data, **dict.fromkeys(ids, now if now is not None else __import__("time").time())})

    def _read_observation_handled(self) -> dict[str, float]:
        handled: dict[str, float] = {}
        for key, value in read_json_file(self.observation_handled_path).items():
            try:
                handled[str(key)] = float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return handled


def _observation_event(request: _ObservationEventInput) -> ObservationEvent:
    kwargs = request.kwargs
    refs = kwargs.get("evidence_refs") or []
    return ObservationEvent(
        observation_id=new_id("obs"),
        thread_id=request.thread_id,
        event_type=str(request.event_type or "observation"),
        summary=str(request.summary or ""),
        urgency=str(kwargs.get("urgency") or "normal"),
        severity=str(kwargs.get("severity") or ""),
        source_agent_id=str(kwargs.get("source_agent_id") or ""),
        parent_agent_id=str(kwargs.get("parent_agent_id") or ""),
        root_task_id=str(kwargs.get("root_task_id") or ""),
        evidence_refs=tuple(str(item) for item in refs),
        requires_main_agent=bool(kwargs.get("requires_main_agent")),
        requires_llm_report=bool(kwargs.get("requires_llm_report")),
        observed_at=request.current,
        metadata=kwargs.get("metadata") or {},
    )
