# LLM: Compact apply work-state helpers; never invent facts that are missing from source files.
# 模块用途: 生成 compact apply 的工作状态快照和摘要，供后续 resume 做一致性对照。

from __future__ import annotations

"""work-state snapshot helpers for compact apply."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_tool_output_refs import tool_output_artifact_refs
from .compact_work_state_archive import source_work_state
from .compact_work_state_sources import WorkStateFieldSourceRequest, build_work_state_field_sources
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_WORK_STATE_SNAPSHOT_SCHEMA = RuntimeMemorySchemaOptions("compact_work_state_snapshot")


# LLM: WorkStateSnapshotRequest keeps compact apply work-state construction bundle-based.
# 类用途: 汇总生成 work_state_snapshot 所需的 plan、restore refs、路径、时间和 apply 标识。
@dataclass(frozen=True)
class WorkStateSnapshotRequest:
    plan: dict[str, Any]
    restore_refs: dict[str, Any]
    paths: dict[str, Path]
    now: str
    apply_id: str
    plan_id: str


# LLM: build_work_state_snapshot captures state signals for future manual resume consistency checks.
# 函数用途: 生成 compact apply 的工作状态快照，固定目标、下一步、约束、引用和测试状态的可恢复字段。
def build_work_state_snapshot(request: WorkStateSnapshotRequest) -> dict[str, Any]:
    source_state = source_work_state(request.restore_refs, request.plan["scope"])
    snapshot = _base_snapshot(request, source_state)
    snapshot["completeness"] = _work_state_completeness(snapshot, request.restore_refs)
    snapshot["missing_fields"] = _missing_fields(snapshot["completeness"])
    snapshot["source_quality"] = _source_quality(snapshot, request.restore_refs)
    return snapshot


# LLM: work_state_summary gives apply_bundle enough state signal without duplicating the full snapshot.
# 函数用途: 提取工作状态快照的关键字段，供 apply bundle 和 CLI 后续展示。
def work_state_summary(work_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal_present": bool(work_state["goal"]),
        "next_actions_count": len(work_state["next_actions"]),
        "missing_fields": list(work_state["missing_fields"]),
        "source_quality": dict(work_state["source_quality"]),
    }


# LLM: restore_refs_summary gives callers counts without opening restore_refs.json.
# 函数用途: 统计恢复引用包里三类原始事实源的数量，供 apply bundle 和 work state 快速展示。
def restore_refs_summary(restore_refs: dict[str, Any]) -> dict[str, int]:
    refs = restore_refs["source_refs"]
    return {
        "archive_files": len(refs["archive_files"]),
        "snapshot_files": len(refs["snapshot_files"]),
        "token_ledgers": len(refs["token_ledgers"]),
    }


# LLM: _base_snapshot records known fields and marks unknown fields explicitly.
# 函数用途: 组装 work_state_snapshot 的固定字段，不把缺失验收、约束或测试状态伪造成已知。
def _base_snapshot(request: WorkStateSnapshotRequest, source_state: dict[str, Any]) -> dict[str, Any]:
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
        # LLM: runtime_handoff is soft resume context, not an acceptance gate.
        "runtime_handoff": field_sources.runtime_handoff,
        "reserved": runtime_memory_reserved_fields(COMPACT_WORK_STATE_SNAPSHOT_SCHEMA),
    }


def _task_progress_next_action(progress: dict[str, Any]) -> str:
    return str(progress.get("next_action") or "").strip() if isinstance(progress, dict) else ""


# LLM: recent runtime guidance outranks stale progress when compact resumes an active task.
# 函数用途: 如果运行中有新 guidance，续接包优先提示新指导，避免 compact 后继续执行旧 next_step。
def _runtime_guidance_next_action(handoff: dict[str, Any]) -> str:
    if not isinstance(handoff, dict):
        return ""
    rows = handoff.get("recent_guidance")
    if not isinstance(rows, list) or not rows:
        return ""
    first = rows[0] if isinstance(rows[0], dict) else {}
    message = str(first.get("message") or "").strip()
    return f"按最近运行中提示继续：{message}" if message else ""


# LLM: _work_state_restore_refs keeps source path existence checks close to work-state capture.
# 函数用途: 写入 restore refs 路径、源计数和所有源路径是否仍存在。
def _work_state_restore_refs(paths: dict[str, Path], restore_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(paths["restore_refs_json"]),
        "source_counts": restore_refs_summary(restore_refs),
        "all_source_paths_exist": _all_source_paths_exist(restore_refs),
    }


# LLM: _work_state_refs keeps the snapshot connected to compact outputs and source counts.
# 函数用途: 汇总 work state 后续恢复要读取的 apply 文件和 source refs 计数。
def _work_state_refs(paths: dict[str, Path], restore_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "compact_context": str(paths["context_md"]),
        "apply_bundle": str(paths["apply_bundle_json"]),
        "restore_refs": str(paths["restore_refs_json"]),
        "source_counts": restore_refs_summary(restore_refs),
    }


# LLM: _work_state_completeness reports what is present without making soft gaps fatal for manual apply.
# 函数用途: 标记工作状态快照中目标、下一步、引用、验收、约束和测试状态是否已经有事实源支持。
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


# LLM: _missing_fields is the explicit contract for future Action Guard and Drift Detector checks.
# 函数用途: 将缺失的工作状态字段列出来，避免后续 resume 把 unknown 当成已知。
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


# LLM: _source_quality summarizes whether the snapshot is enough for unattended resume.
# 函数用途: 给 work state 标记来源质量；缺少验收、约束或测试时保持 partial。
def _source_quality(snapshot: dict[str, Any], restore_refs: dict[str, Any]) -> dict[str, Any]:
    missing = _missing_fields(snapshot["completeness"])
    return {
        "status": "complete" if not missing else "partial",
        "missing_fields": missing,
        "source_counts": restore_refs_summary(restore_refs),
    }


# LLM: _all_source_paths_exist checks restore refs without reading bodies.
# 函数用途: 确认 restore refs 中登记的原始路径仍存在，供 self-check 阻断缺失事实源。
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
