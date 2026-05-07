# LLM: Compact apply work-state helpers; never invent facts that are missing from source files.
# 模块用途: 生成手动 compact apply 的工作状态快照和摘要，供后续 resume 做一致性对照。

from __future__ import annotations

"""work-state snapshot helpers for compact apply."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    source_state = _source_work_state(request.restore_refs)
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
    next_actions = source_state["next_actions"]
    field_sources = build_work_state_field_sources(WorkStateFieldSourceRequest(request.plan, source_state))
    return {
        "version": COMPACT_WORK_STATE_SNAPSHOT_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_WORK_STATE_SNAPSHOT_SCHEMA),
        "event_type": "compact_work_state_snapshot",
        "apply_id": request.apply_id,
        "plan_id": request.plan_id,
        "workspace_root": request.plan["workspace_root"],
        "scope": request.plan["scope"],
        "created_at": request.now,
        "goal": source_state["goal"],
        "phase": "manual_compact_apply",
        "next_step": next_actions[0] if next_actions else "",
        "next_actions": next_actions,
        "acceptance": field_sources.acceptance,
        "constraints": field_sources.constraints,
        "changed_files": [],
        "read_files": field_sources.read_files,
        "artifact_refs": [],
        "restore_refs": _work_state_restore_refs(request.paths, request.restore_refs),
        "refs": _work_state_refs(request.paths, request.restore_refs),
        "git_state": {"status": "not_captured", "changed_files": []},
        "latest_tests": field_sources.latest_tests,
        "reserved": runtime_memory_reserved_fields(COMPACT_WORK_STATE_SNAPSHOT_SCHEMA),
    }


# LLM: _source_work_state extracts goal and next actions from existing snapshot fact sources only.
# 函数用途: 从 restore refs 指向的 snapshot 中提取最小工作状态，不猜测未记录的验收或约束。
def _source_work_state(restore_refs: dict[str, Any]) -> dict[str, Any]:
    for ref in restore_refs["source_refs"]["snapshot_files"]:
        payload = _read_json_dict(Path(ref["path"]))
        if payload:
            return {
                "goal": _first_text(payload.get("user_intents")),
                "next_actions": _text_list(payload.get("next_actions")),
                "content_paths": _text_list(payload.get("content_paths")),
                "task_refs": _text_list(payload.get("task_refs")),
            }
    return {"goal": "", "next_actions": [], "content_paths": [], "task_refs": []}


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


# LLM: _read_json_dict is a best-effort reader for existing fact-source snapshots.
# 函数用途: 读取 JSON 对象，失败时返回空 dict，避免 compact apply 因单个旧文件损坏而崩溃。
def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _first_text extracts the first useful text field from a list-like snapshot field.
# 函数用途: 从 snapshot 的 user_intents 等字段中提取第一条非空文本。
def _first_text(value: Any) -> str:
    items = _text_list(value)
    return items[0] if items else ""


# LLM: _text_list normalizes old snapshot list fields into short strings.
# 函数用途: 将任意列表字段转成去空白字符串列表，避免 work_state 写入非文本对象。
def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


__all__ = [
    "COMPACT_WORK_STATE_SNAPSHOT_SCHEMA",
    "WorkStateSnapshotRequest",
    "build_work_state_snapshot",
    "restore_refs_summary",
    "work_state_summary",
]
