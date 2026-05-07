from __future__ import annotations

"""LLM: append-only daily event ledger for runtime memory.

Human version:
The daily ledger is an index of what happened today. It stores compact task/run
facts and file references, not full subagent context or tool output bodies.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from ._storage_dates import _date_key


@dataclass(frozen=True)
class DailyLedgerAppendResult:
    """Path and id for one appended daily event."""

    events_jsonl: Path
    event_id: str


@dataclass(frozen=True)
class DailyLedgerWorkspaceRefs:
    """Task/run workspace roots referenced by a compact daily event."""

    task_workspace_root: Path
    agent_run_workspace_root: Path
    task_artifact_manifest_jsonl: Path | None = None
    # LLM: manifest refs point to summaries/hashes, never artifact bodies.
    agent_artifact_manifest_jsonl: Path | None = None


def daily_events_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:
    """Return `daily/YYYY-MM-DD/events.jsonl` under the runtime memory root."""

    return Path(root) / "daily" / _date_key(created_at) / "events.jsonl"


def append_subagent_task_event(
    root: str | Path,
    task: Any,
    *,
    workspace_refs: DailyLedgerWorkspaceRefs,
    now: float,
) -> DailyLedgerAppendResult:
    """Append a compact subagent task/run event to the daily ledger."""

    path = daily_events_path_for(root, now)
    payload = _event_payload(task, workspace_refs, now)
    append_jsonl(path, payload, sort_keys=True)
    return DailyLedgerAppendResult(events_jsonl=path, event_id=str(payload["event_id"]))


def _event_payload(task: Any, workspace_refs: DailyLedgerWorkspaceRefs, now: float) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "version": 1,
        "event_id": _event_id(task_id, run_id, now),
        "event_type": "subagent_task_saved",
        "created_at": _utc_iso(now),
        "task_id": task_id,
        "run_id": run_id,
        "parent_run_id": str(getattr(task, "parent_id", "")),
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "duration_seconds": _duration_seconds(task, now),
        "summary": _summary(task),
        "refs": _refs(task, workspace_refs),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "search": _search_fields(task),
    }


def _refs(task: Any, workspace_refs: DailyLedgerWorkspaceRefs) -> dict[str, str]:
    legacy_task_dir = str(getattr(task, "task_dir", ""))
    task_workspace_root = workspace_refs.task_workspace_root
    agent_run_workspace_root = workspace_refs.agent_run_workspace_root
    return {
        "task_workspace": str(task_workspace_root),
        "task_state": str(task_workspace_root / "state.json"),
        "task_timeline": str(task_workspace_root / "timeline.jsonl"),
        "agent_run_workspace": str(agent_run_workspace_root),
        "agent_run_state": str(agent_run_workspace_root / "state.json"),
        "agent_run_timeline": str(agent_run_workspace_root / "timeline.jsonl"),
        "task_artifact_manifest": _path_text(workspace_refs.task_artifact_manifest_jsonl),
        "agent_artifact_manifest": _path_text(workspace_refs.agent_artifact_manifest_jsonl),
        "legacy_task_dir": legacy_task_dir,
        "legacy_task_json": str(Path(legacy_task_dir) / "task.json") if legacy_task_dir else "",
        "legacy_checkpoint": str(getattr(task, "checkpoint_json", "")),
        "legacy_status_report": str(getattr(task, "status_report_json", "")),
    }


def _search_fields(task: Any) -> dict[str, object]:
    return {
        "agent_name": str(getattr(task, "agent_name", "")),
        "role": str(getattr(task, "role", "")),
        "status": str(getattr(task, "status", "")),
        "current_step": str(getattr(task, "current_step", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
    }


def _path_text(path: Path | None) -> str:
    return str(path) if path else ""


def _summary(task: Any) -> str:
    latest = str(getattr(task, "latest_summary", "")).strip()
    if latest:
        return latest[:500]
    current = str(getattr(task, "current_step", "")).strip()
    return current[:500] if current else str(getattr(task, "status", ""))[:500]


def _duration_seconds(task: Any, now: float) -> float:
    created_at = float(getattr(task, "created_at", 0.0) or now)
    return max(0.0, round(now - created_at, 3))


def _event_id(task_id: str, run_id: str, now: float) -> str:
    return f"evt-{_safe_segment(task_id)}-{_safe_segment(run_id)}-{int(now * 1000)}"


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_segment(value: str) -> str:
    return str(value or "item").replace("/", "_").replace("\\", "_").strip() or "item"


__all__ = [
    "DailyLedgerAppendResult",
    "DailyLedgerWorkspaceRefs",
    "append_subagent_task_event",
    "daily_events_path_for",
]
