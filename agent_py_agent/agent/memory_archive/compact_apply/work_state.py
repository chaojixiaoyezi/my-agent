
from __future__ import annotations

"""work-state snapshot helpers for compact apply."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.value_parsing import dedupe_strings
from ..compact_tool_output_refs import tool_call_refs, tool_output_artifact_refs
from ..schema import (
    RuntimeMemorySchemaOptions,
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
    artifact_refs = tool_output_artifact_refs(request.restore_refs)
    tool_progress = _dedupe_tool_progress([
        *_tool_progress_from_call_refs(tool_call_refs(request.restore_refs)),
        *_tool_progress_from_artifact_refs(artifact_refs),
    ])
    guidance_next = _runtime_guidance_next_action(field_sources.runtime_handoff)
    progress_next = _task_progress_next_action(field_sources.task_progress)
    tool_next = _tool_progress_next_action(tool_progress)
    next_actions = (
        ([guidance_next] if guidance_next else [])
        or ([progress_next] if progress_next else [])
        or ([tool_next] if tool_next else [])
        or source_state["next_actions"]
        or field_sources.next_actions
    )
    read_files = dedupe_strings([
        *field_sources.read_files,
        *_tool_read_files_from_progress(tool_progress),
    ])
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
        "read_files": read_files,
        "tool_progress": tool_progress,
        "artifact_refs": artifact_refs,
        "restore_refs": _work_state_restore_refs(request.paths, request.restore_refs),
        "refs": _work_state_refs(request.paths, request.restore_refs),
        "git_state": {"status": "not_captured", "changed_files": []},
        "latest_tests": field_sources.latest_tests,
        "task_progress": field_sources.task_progress,
        "desired_outputs": field_sources.desired_outputs,
        "run_intent": field_sources.run_intent,
        "runtime_handoff": field_sources.runtime_handoff,
        "source_load_errors": _source_load_errors(source_state),
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


def _tool_progress_from_artifact_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    progress: list[dict[str, Any]] = []
    for ref in refs:
        if item := _tool_progress_item(ref):
            progress.append(item)
    return progress[-24:]


def _tool_progress_from_call_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    progress: list[dict[str, Any]] = []
    for ref in refs:
        if item := _tool_progress_item(ref):
            progress.append(item)
    return progress[-48:]


def _dedupe_tool_progress(progress: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    for item in progress:
        tool = str(item.get("tool") or "").strip()
        source_path = str(item.get("source_path") or "").strip()
        if not tool or not source_path:
            continue
        key = (tool, source_path)
        if key in positions:
            existing = deduped[positions[key]]
            if not str(existing.get("scoped_call_id") or "").strip():
                existing["scoped_call_id"] = str(item.get("scoped_call_id") or "").strip()
            if not int(existing.get("size_bytes", 0) or 0):
                existing["size_bytes"] = int(item.get("size_bytes", 0) or 0)
            continue
        positions[key] = len(deduped)
        deduped.append(dict(item))
    return deduped[-48:]


def _tool_progress_item(ref: dict[str, Any]) -> dict[str, Any]:
    tool = str(ref.get("tool") or "").strip()
    if tool not in {"read_file", "list_files", "find_files", "run_command", "read_artifact", "write_file"}:
        return {}
    parameters = ref.get("parameters", {}) if isinstance(ref.get("parameters"), dict) else {}
    source_path = str(ref.get("source_path") or parameters.get("path") or parameters.get("command") or "").strip()
    if not source_path:
        return {}
    return {
        "tool": tool,
        "source_path": source_path,
        "scoped_call_id": str(ref.get("scoped_call_id") or ref.get("call_id") or "").strip(),
        "size_bytes": int(ref.get("size_bytes", 0) or 0),
    }


def _tool_read_files_from_progress(progress: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("source_path") or "")
        for item in progress
        if str(item.get("tool") or "") in {"read_file", "read_artifact"}
    ]


def _tool_progress_next_action(progress: list[dict[str, Any]]) -> str:
    if not progress:
        return ""
    reads = [str(item.get("source_path") or "") for item in progress if item.get("tool") in {"read_file", "read_artifact"}]
    scans = [str(item.get("source_path") or "") for item in progress if item.get("tool") in {"list_files", "find_files", "run_command"}]
    if not reads and not scans:
        return ""
    recent = dedupe_strings([*reads[-6:], *scans[-4:]])[-8:]
    recent_text = "；".join(recent)
    action = (
        f"已从本轮工具记录恢复到：已读取 {len(dedupe_strings(reads))} 个文件/Artifact、"
        f"查看 {len(dedupe_strings(scans))} 次目录或命令。"
        "先判断已读集合是否已覆盖当前任务；如果已覆盖，直接基于已读内容写入 output 并提交验收。"
        "不要为确认起点而重读 START/README/索引文件，也不要反复读取尚未创建的 final_report 或 compact 状态文件；"
        "只有发现明确缺口时才补读。"
    )
    return action + (f" 最近线索：{recent_text}" if recent_text else "")


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
    paths = [
        str(item.get("path") or "")
        for group in restore_refs["source_refs"].values()
        for item in group
        if isinstance(item, dict)
    ]
    return all(Path(path).exists() for path in paths if path)


__all__ = [
    "COMPACT_WORK_STATE_SNAPSHOT_SCHEMA",
    "WorkStateSnapshotRequest",
    "build_work_state_snapshot",
    "restore_refs_summary",
    "work_state_summary",
]
