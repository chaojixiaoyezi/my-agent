
from __future__ import annotations

"""work-state snapshot helpers for compact apply."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..compact_tool_output_refs import tool_output_artifact_refs
from ..schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_WORK_STATE_SNAPSHOT_SCHEMA = RuntimeMemorySchemaOptions("compact_work_state_snapshot")


@dataclass(frozen=True)
class WorkStateSnapshotRequest:
    plan: dict[str, Any]
    restore_refs: dict[str, Any]
    paths: dict[str, Path]
    now: str
    apply_id: str
    plan_id: str


def build_work_state_snapshot(request: WorkStateSnapshotRequest) -> dict[str, Any]:
    from ..compact_work_state.archive import source_work_state

    source_state = source_work_state(request.restore_refs, request.plan["scope"])
    snapshot = _base_snapshot(request, source_state)
    snapshot["completeness"] = _work_state_completeness(snapshot, request.restore_refs)
    snapshot["missing_fields"] = _missing_fields(snapshot["completeness"])
    snapshot["source_quality"] = _source_quality(snapshot, request.restore_refs)
    return snapshot


def work_state_summary(work_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal_present": bool(work_state["goal"]),
        "next_actions_count": len(work_state["next_actions"]),
        "missing_fields": list(work_state["missing_fields"]),
        "source_quality": dict(work_state["source_quality"]),
    }


def restore_refs_summary(restore_refs: dict[str, Any]) -> dict[str, int]:
    refs = restore_refs["source_refs"]
    return {
        "archive_files": len(refs["archive_files"]),
        "snapshot_files": len(refs["snapshot_files"]),
        "token_ledgers": len(refs["token_ledgers"]),
    }


def _base_snapshot(request: WorkStateSnapshotRequest, source_state: dict[str, Any]) -> dict[str, Any]:
    from ..compact_work_state.sources import (
        WorkStateFieldSourceRequest,
        build_work_state_field_sources,
    )

    field_sources = build_work_state_field_sources(WorkStateFieldSourceRequest(request.plan, source_state))
    goal = source_state["goal"] or field_sources.goal
    guidance_next = _runtime_guidance_next_action(field_sources.runtime_handoff)
    progress_next = _task_progress_next_action(field_sources.task_progress)
    next_actions = (
        ([guidance_next] if guidance_next else [])
        or ([progress_next] if progress_next else [])
        or source_state["next_actions"]
        or field_sources.next_actions
    )
    return {
        "version": COMPACT_WORK_STATE_SNAPSHOT_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_WORK_STATE_SNAPSHOT_SCHEMA),
        "event_type": "compact_work_state_snapshot",
        "apply_id": request.apply_id,
        "plan_id": request.plan_id,
        "workspace_root": request.plan["workspace_root"],
        "scope": request.plan["scope"],
        "created_at": request.now,
        "goal": goal,
        "phase": "compact_apply",
        "next_step": next_actions[0] if next_actions else "",
        "next_actions": next_actions,
        "acceptance": field_sources.acceptance,
        "constraints": field_sources.constraints,
        "changed_files": [],
        "read_files": field_sources.read_files,
        "artifact_refs": tool_output_artifact_refs(request.restore_refs),
        "restore_refs": _work_state_restore_refs(request.paths, request.restore_refs),
        "refs": _work_state_refs(request.paths, request.restore_refs),
        "git_state": {"status": "not_captured", "changed_files": []},
        "latest_tests": field_sources.latest_tests,
        "task_progress": field_sources.task_progress,
        "desired_outputs": field_sources.desired_outputs,
        "run_intent": field_sources.run_intent,
        "runtime_handoff": field_sources.runtime_handoff,
        "source_load_errors": _source_load_errors(source_state),
        "reserved": runtime_memory_reserved_fields(COMPACT_WORK_STATE_SNAPSHOT_SCHEMA),
    }


def _task_progress_next_action(progress: dict[str, Any]) -> str:
    return str(progress.get("next_action") or "").strip() if isinstance(progress, dict) else ""


def _runtime_guidance_next_action(handoff: dict[str, Any]) -> str:
    if not isinstance(handoff, dict):
        return ""
    rows = handoff.get("recent_guidance")
    if not isinstance(rows, list) or not rows:
        return ""
    first = rows[0] if isinstance(rows[0], dict) else {}
    message = str(first.get("message") or "").strip()
    return f"按最近运行中提示继续：{message}" if message else ""


def _work_state_restore_refs(paths: dict[str, Path], restore_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(paths["restore_refs_json"]),
        "source_counts": restore_refs_summary(restore_refs),
        "all_source_paths_exist": _all_source_paths_exist(restore_refs),
    }


def _work_state_refs(paths: dict[str, Path], restore_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "compact_context": str(paths["context_md"]),
        "apply_bundle": str(paths["apply_bundle_json"]),
        "restore_refs": str(paths["restore_refs_json"]),
        "source_counts": restore_refs_summary(restore_refs),
    }


def _work_state_completeness(snapshot: dict[str, Any], restore_refs: dict[str, Any]) -> dict[str, bool]:
    return {
        "goal_present": bool(snapshot["goal"]),
        "next_actions_present": bool(snapshot["next_actions"]),
        "source_refs_present": any(restore_refs_summary(restore_refs).values()),
        "restore_refs_exist": bool(snapshot["restore_refs"]["all_source_paths_exist"]),
        "acceptance_present": bool(snapshot["acceptance"]["items"]),
        "constraints_present": bool(snapshot["constraints"]["items"]),
        "test_state_present": bool(snapshot["latest_tests"]["items"]),
    }


def _missing_fields(completeness: dict[str, bool]) -> list[str]:
    names = {
        "goal_present": "goal",
        "next_actions_present": "next_step",
        "source_refs_present": "restore_refs",
        "acceptance_present": "acceptance",
        "constraints_present": "constraints",
        "test_state_present": "latest_tests",
    }
    return [field for key, field in names.items() if not completeness[key]]


def _source_quality(snapshot: dict[str, Any], restore_refs: dict[str, Any]) -> dict[str, Any]:
    missing = _missing_fields(snapshot["completeness"])
    return {
        "status": "complete" if not missing else "partial",
        "missing_fields": missing,
        "source_counts": restore_refs_summary(restore_refs),
    }


def _source_load_errors(source_state: dict[str, Any]) -> list[dict[str, object]]:
    value = source_state.get("source_load_errors")
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _all_source_paths_exist(restore_refs: dict[str, Any]) -> bool:
    paths = [item["path"] for group in restore_refs["source_refs"].values() for item in group]
    return all(Path(path).exists() for path in paths if path)


__all__ = [
    "COMPACT_WORK_STATE_SNAPSHOT_SCHEMA",
    "WorkStateSnapshotRequest",
    "build_work_state_snapshot",
    "restore_refs_summary",
    "work_state_summary",
]
