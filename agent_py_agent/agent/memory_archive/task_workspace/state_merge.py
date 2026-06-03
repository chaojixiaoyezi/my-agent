from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .payloads import state_payload


@dataclass(frozen=True)
class TaskStateMergeRequest:
    task_id: str
    run_id: str
    task: Any
    now: float
    previous_state: dict[str, object]


def next_task_state(request: TaskStateMergeRequest) -> dict[str, object]:
    if request.run_id == request.task_id or not request.previous_state:
        return state_payload(request.task_id, request.run_id, request.task, request.now)
    payload = dict(request.previous_state)
    payload["task_id"] = str(payload.get("task_id") or request.task_id)
    payload["child_run_ids"] = _unique_strings([*list(payload.get("child_run_ids") or []), request.run_id])
    payload["updated_at"] = request.now
    return payload


def _unique_strings(values: list[object]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item or "").strip()))
