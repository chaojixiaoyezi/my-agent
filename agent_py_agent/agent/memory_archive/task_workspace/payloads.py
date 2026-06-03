from __future__ import annotations

from pathlib import Path
from typing import Any

from ...common.json_io import append_jsonl_records, read_json_object, write_json_object


def state_payload(task_id: str, run_id: str, task: Any, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "primary_run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "child_run_ids": list(getattr(task, "child_ids", []) or []),
        "updated_at": now,
        "legacy": {
            "task_dir": str(getattr(task, "task_dir", "")),
            "task_json": str(Path(str(getattr(task, "task_dir", ""))) / "task.json")
            if getattr(task, "task_dir", "")
            else "",
        },
    }


def timeline_event(task: Any, now: float, previous_state: dict[str, object]) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "ts": now,
        "event": "task_workspace_synced",
        "task_id": task_id,
        "run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "previous_status": str(previous_state.get("status") or ""),
        "summary": str(getattr(task, "latest_summary", "")),
        "refs": {
            "legacy_task_dir": str(getattr(task, "task_dir", "")),
        },
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    write_json_object(path, payload, sort_keys=False)


def append_timeline(path: Path, payload: dict[str, object]) -> None:
    append_jsonl_records(path, [payload])


__all__ = [
    "append_timeline",
    "read_json_object",
    "state_payload",
    "timeline_event",
    "write_json",
]
