# LLM: payload 只投影 canonical task/run 字段，不拥有状态迁移或目录授权；写入由明确的归档调用方负责。
# 模块用途: 构建任务状态、时间线和归档引用，供工作区同步使用，不从摘要文字猜状态。
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...common.json_io import append_jsonl_records, read_json_object


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
            "task_workspace": str(getattr(task, "task_workspace_dir", "")),
            "agent_run_workspace": str(getattr(task, "agent_run_workspace_dir", "")),
        },
    }


def append_timeline(path: Path, payload: dict[str, object]) -> None:
    if _last_event_signature(path) == _event_signature(payload):
        return
    append_jsonl_records(path, [payload])


def _last_event_signature(path: Path) -> tuple[object, ...] | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return _event_signature(payload) if isinstance(payload, dict) else None
    return None


def _event_signature(payload: dict[str, object]) -> tuple[object, ...]:
    return (
        payload.get("event"),
        payload.get("task_id"),
        payload.get("run_id"),
        payload.get("status"),
        payload.get("summary"),
        tuple(sorted((payload.get("refs") or {}).items())) if isinstance(payload.get("refs"), dict) else (),
    )


__all__ = [
    "append_timeline",
    "read_json_object",
    "state_payload",
    "timeline_event",
]
