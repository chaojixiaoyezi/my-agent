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


# LLM: _source_work_state prefers authoritative snapshot files, then falls back to bounded run archives.
# 函数用途: 从 restore refs 指向的 snapshot/hook/raw 文件提取目标和下一步，不猜测未记录的验收或约束。
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
    archive_state = _archive_work_state(restore_refs)
    if archive_state["goal"] or archive_state["next_actions"]:
        return archive_state
    return {"goal": "", "next_actions": [], "content_paths": [], "task_refs": []}


# LLM: _archive_work_state reads only restore_refs archive files to recover real-run goal and next action.
# 函数用途: 当没有 snapshot JSON 文件时，从 hook recovery snapshot 或 raw 用户事件回填最小工作状态。
def _archive_work_state(restore_refs: dict[str, Any]) -> dict[str, Any]:
    records = _archive_records(restore_refs)
    return {
        "goal": _archive_goal(records),
        "next_actions": _archive_next_actions(records),
        "content_paths": _archive_texts(records, "content_paths"),
        "task_refs": _archive_task_refs(records),
    }


# LLM: _archive_records keeps fallback reads scoped to compact restore refs instead of scanning workspace.
# 函数用途: 读取 restore_refs 已登记的 raw/hook JSONL 文件，跳过损坏行并保持文件顺序。
def _archive_records(restore_refs: dict[str, Any]) -> list[dict[str, Any]]:
    refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs.get("source_refs"), dict) else {}
    archive_refs = refs.get("archive_files", []) if isinstance(refs.get("archive_files"), list) else []
    return [record for ref in archive_refs for record in _read_jsonl_dicts(Path(str(ref.get("path", ""))))]


# LLM: _archive_goal prefers recovery hook user_intents, then raw user message previews.
# 函数用途: 从真实 run 归档里提取用户目标，让 compact resume 不因缺 snapshot 文件丢失目标。
def _archive_goal(records: list[dict[str, Any]]) -> str:
    return _first_nonempty([_first_text(record.get("user_intents")) for record in records]) or _first_nonempty(
        [_raw_user_text(record) for record in records]
    )


# LLM: _raw_user_text returns raw user message text without interpreting assistant output as task facts.
# 函数用途: 从 raw archive 用户事件提取目标文本，非用户事件返回空字符串。
def _raw_user_text(record: dict[str, Any]) -> str:
    if str(record.get("speaker") or "") != "user":
        return ""
    return str(record.get("content") or record.get("content_preview") or "").strip()


# LLM: _first_nonempty keeps archive fallback selectors flat enough for guardrail limits.
# 函数用途: 返回字符串列表中的第一条非空值，避免字段提取函数出现深层嵌套。
def _first_nonempty(values: list[str]) -> str:
    return next((value for value in values if value), "")


# LLM: _archive_next_actions uses hook recovery next_actions without parsing assistant prose.
# 函数用途: 从 hook recovery snapshot 回填下一步；没有结构化 next_actions 时保持未知。
def _archive_next_actions(records: list[dict[str, Any]]) -> list[str]:
    for record in records:
        items = _text_list(record.get("next_actions"))
        if items:
            return items
    return []


# LLM: _archive_texts collects list-like fields from archive records for bounded fact-source roots.
# 函数用途: 提取 content_paths 等列表字段，供 work_state 后续扫描任务事实源使用。
def _archive_texts(records: list[dict[str, Any]], key: str) -> list[str]:
    return _dedupe([item for record in records for item in _text_list(record.get(key))])


# LLM: _archive_task_refs combines hook task_refs with raw task/run ids for workspace fact-source lookup.
# 函数用途: 收集真实 run 相关 task/run 标识，帮助后续查找 task/subagent fact files。
def _archive_task_refs(records: list[dict[str, Any]]) -> list[str]:
    values = [item for record in records for item in _text_list(record.get("task_refs"))]
    for record in records:
        values.extend(str(record.get(key) or "").strip() for key in ("task_id", "run_id") if record.get(key))
    return _dedupe(values)


# LLM: _read_jsonl_dicts is a tolerant reader for append-only raw/hook archive files.
# 函数用途: 逐行读取 JSONL 对象，坏行按缺失处理，避免 compact apply 被旧归档中断。
def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


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


# LLM: _dedupe preserves source order for recovered archive fields.
# 函数用途: 对 hook/raw 回填字段去重，避免同一 task/run id 多次进入事实源扫描。
def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


__all__ = [
    "COMPACT_WORK_STATE_SNAPSHOT_SCHEMA",
    "WorkStateSnapshotRequest",
    "build_work_state_snapshot",
    "restore_refs_summary",
    "work_state_summary",
]
