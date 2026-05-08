# LLM: Compact resume turns a non-destructive compact apply bundle into a manual recovery context.
# 模块用途: 读取 compact apply 产物并生成手动 resume 上下文、一致性报告和推荐读取路径。

from __future__ import annotations

"""manual resume support for memory-compact --apply artifacts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_action_guard import (
    CompactActionGuardOptions,
    CompactActionGuardRequest,
    build_compact_action_guard,
)
from .compact_resume_failsafe import collect_fail_safe_checkpoints
from .compact_resume_handoff import (
    CompactResumeHandoffRequest,
    build_compact_resume_handoff,
    render_compact_resume_context_block,
)
from .compact_resume_io import (
    read_compact_apply_artifacts,
    read_json_object,
    resolve_compact_metadata_path,
)
from .compact_subagent_owner import (
    CompactSubagentOwnerRequest,
    resolve_compact_subagent_owner,
)
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_RESUME_SCHEMA = RuntimeMemorySchemaOptions("compact_resume")
COMPACT_RESUME_CONSISTENCY_SCHEMA = RuntimeMemorySchemaOptions("compact_resume_consistency_report")


# LLM: MemoryCompactResumeOptions is the bundle for manual compact resume and future subagent compact owners.
# 类用途: 描述要恢复的 compact apply 引用、调用者类型和未来子代理会话压缩的预留范围。
@dataclass(frozen=True)
class MemoryCompactResumeOptions:
    apply_ref: str
    owner_type: str = "main_agent"
    owner_id: str = ""
    resume_mode: str = "manual"


# LLM: _ResumePayloadBuildRequest keeps compact resume rendering extensible without long helper signatures.
# 类用途: 汇总恢复 payload 生成所需字段，后续加入 subagent session compact 状态时优先扩展这里。
@dataclass(frozen=True)
class _ResumePayloadBuildRequest:
    workspace: Path
    options: MemoryCompactResumeOptions
    metadata: dict[str, Any]
    artifacts: dict[str, Any]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]


# LLM: _BlockedResumeRequest keeps failed compact resolution payloads on the same bundle style.
# 类用途: 汇总缺失 compact metadata 时的阻断结果字段，避免错误路径 helper 参数继续增长。
@dataclass(frozen=True)
class _BlockedResumeRequest:
    workspace: Path
    options: MemoryCompactResumeOptions
    metadata_path: Path
    status: str


# LLM: build_memory_compact_resume is read-only; it never mutates archive, task, or subagent files.
# 函数用途: 从 apply_id 或 apply 产物路径读取恢复包，生成可人工注入的恢复上下文和一致性报告。
def build_memory_compact_resume(root: str | Path, options: MemoryCompactResumeOptions) -> dict[str, Any]:
    workspace = Path(root)
    metadata_path = resolve_compact_metadata_path(workspace, options.apply_ref)
    metadata = read_json_object(metadata_path)
    if not metadata:
        return _blocked_result(_BlockedResumeRequest(workspace, options, metadata_path, "blocked_missing_compact_metadata"))
    artifacts = read_compact_apply_artifacts(metadata)
    consistency = _consistency_report(metadata, artifacts, options)
    action_guard = build_compact_action_guard(
        CompactActionGuardRequest(
            consistency_report=consistency,
            work_state=artifacts["work_state"],
            refs=metadata.get("refs", {}),
            options=CompactActionGuardOptions(
                mode=options.resume_mode,
                owner_type=options.owner_type,
                owner_id=options.owner_id,
            ),
        )
    )
    return _resume_payload(_ResumePayloadBuildRequest(workspace, options, metadata, artifacts, consistency, action_guard))


# LLM: _consistency_report is the manual resume gate before any future automated action guard.
# 函数用途: 对照 metadata、apply bundle、restore refs、work state 和 self-check，判断恢复包是否可继续使用。
def _consistency_report(
    metadata: dict[str, Any], artifacts: dict[str, Any], options: MemoryCompactResumeOptions
) -> dict[str, Any]:
    checks = _consistency_checks(metadata, artifacts)
    hard_ok = all(item["ok"] for item in checks if item["severity"] == "hard")
    return {
        "version": COMPACT_RESUME_CONSISTENCY_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_CONSISTENCY_SCHEMA),
        "ok": hard_ok,
        "status": "ok" if hard_ok else "blocked_needs_human_review",
        "owner": _owner_payload(options),
        "apply_id": metadata.get("apply_id", ""),
        "plan_id": metadata.get("plan_id", ""),
        "checks": checks,
        "missing_fields": _work_state_missing_fields(artifacts),
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_CONSISTENCY_SCHEMA),
    }


# LLM: _consistency_checks keeps hard failures deterministic and soft gaps visible.
# 函数用途: 生成 compact resume 的一致性检查列表；hard 失败阻断，soft 缺口留给人工判断。
def _consistency_checks(metadata: dict[str, Any], artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    work_state = artifacts["work_state"]
    return [
        {"name": "metadata_loaded", "ok": bool(metadata), "severity": "hard"},
        {"name": "apply_bundle_loaded", "ok": bool(artifacts["apply_bundle"]), "severity": "hard"},
        {"name": "restore_refs_loaded", "ok": bool(artifacts["restore_refs"]), "severity": "hard"},
        {"name": "work_state_snapshot_loaded", "ok": bool(work_state), "severity": "hard"},
        {"name": "self_check_loaded", "ok": bool(artifacts["self_check"]), "severity": "hard"},
        {"name": "self_check_ok", "ok": bool(artifacts["self_check"].get("ok")), "severity": "hard"},
        {"name": "apply_ids_consistent", "ok": _artifact_ids_match(metadata, artifacts), "severity": "hard"},
        {"name": "restore_refs_exist", "ok": _restore_refs_exist(artifacts), "severity": "hard"},
        {"name": "compact_context_loaded", "ok": bool(artifacts["compact_context"]), "severity": "hard"},
        {"name": "goal_present", "ok": bool(work_state.get("goal")), "severity": "soft"},
        {"name": "next_step_present", "ok": bool(work_state.get("next_step")), "severity": "soft"},
        {"name": "missing_fields_recorded", "ok": isinstance(work_state.get("missing_fields"), list), "severity": "soft"},
    ]


# LLM: _resume_payload is the stable JSON shape returned by memory-resume --from-compact.
# 函数用途: 组装手动 compact resume 的结果、上下文块、推荐读取路径和未来子代理扩展字段。
def _resume_payload(request: _ResumePayloadBuildRequest) -> dict[str, Any]:
    metadata = request.metadata
    artifacts = request.artifacts
    consistency = request.consistency
    action_guard = request.action_guard
    fail_safe_checkpoints = collect_fail_safe_checkpoints(artifacts["restore_refs"])
    recommended = _recommended_read_paths(metadata, artifacts, fail_safe_checkpoints)
    next_actions = _next_actions(consistency)
    handoff = build_compact_resume_handoff(
        CompactResumeHandoffRequest(
            metadata=metadata,
            work_state=artifacts["work_state"],
            consistency=consistency,
            action_guard=action_guard,
            recommended_read_paths=recommended,
            next_actions=next_actions,
            fail_safe_checkpoints=fail_safe_checkpoints,
        )
    )
    context_block = render_compact_resume_context_block(handoff)
    return {
        "version": COMPACT_RESUME_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_SCHEMA),
        "ok": consistency["ok"],
        "mode": "resume_from_compact",
        "workspace_root": str(request.workspace),
        "apply_id": metadata.get("apply_id", ""),
        "plan_id": metadata.get("plan_id", ""),
        "owner": _owner_payload(request.options),
        "refs": metadata.get("refs", {}),
        "work_state": artifacts["work_state"],
        "consistency_report": consistency,
        "action_guard": action_guard,
        "handoff": handoff,
        "fail_safe_checkpoints": fail_safe_checkpoints,
        "recommended_read_paths": recommended,
        "next_actions": next_actions,
        "context_block": context_block,
        "subagent_session_compact": _subagent_extension(request.workspace, request.options),
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_SCHEMA),
    }


# LLM: _blocked_result returns a valid v2 payload when the requested compact apply cannot be found.
# 函数用途: 生成缺失 metadata 时的阻断结果，CLI 可据此返回非零退出码。
def _blocked_result(request: _BlockedResumeRequest) -> dict[str, Any]:
    consistency = {
        "version": COMPACT_RESUME_CONSISTENCY_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_CONSISTENCY_SCHEMA),
        "ok": False,
        "status": request.status,
        "owner": _owner_payload(request.options),
        "apply_id": request.options.apply_ref,
        "plan_id": "",
        "checks": [{"name": "metadata_loaded", "ok": False, "severity": "hard"}],
        "missing_fields": [],
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_CONSISTENCY_SCHEMA),
    }
    action_guard = build_compact_action_guard(
        CompactActionGuardRequest(
            consistency_report=consistency,
            work_state={},
            refs={"metadata": str(request.metadata_path)},
            options=CompactActionGuardOptions(
                mode=request.options.resume_mode,
                owner_type=request.options.owner_type,
                owner_id=request.options.owner_id,
            ),
        )
    )
    return {
        "version": COMPACT_RESUME_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_SCHEMA),
        "ok": False,
        "mode": "resume_from_compact",
        "workspace_root": str(request.workspace),
        "apply_id": request.options.apply_ref,
        "plan_id": "",
        "owner": _owner_payload(request.options),
        "refs": {"metadata": str(request.metadata_path)},
        "work_state": {},
        "consistency_report": consistency,
        "action_guard": action_guard,
        "handoff": {},
        "fail_safe_checkpoints": [],
        "recommended_read_paths": [str(request.metadata_path)],
        "next_actions": ["Find a valid compact apply id or rerun memory-compact --apply."],
        "context_block": "",
        "subagent_session_compact": _subagent_extension(request.workspace, request.options),
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_SCHEMA),
    }


# LLM: _recommended_read_paths points humans/models back to facts before continuing work.
# 函数用途: 返回手动恢复时必须优先读取的 compact 产物和原始事实源路径。
def _recommended_read_paths(
    metadata: dict[str, Any], artifacts: dict[str, Any], fail_safe_checkpoints: list[dict[str, Any]]
) -> list[str]:
    refs = metadata.get("refs", {}) if isinstance(metadata.get("refs"), dict) else {}
    paths = [str(value) for value in refs.values() if value]
    paths.extend(str(item.get("path", "") or "") for item in fail_safe_checkpoints)
    paths.extend(_source_paths(artifacts["restore_refs"]))
    return _dedupe(paths)


# LLM: _next_actions keeps manual resume from silently executing tools after context recovery.
# 函数用途: 根据一致性结果给出下一步动作；这一步只建议，不自动运行命令或修改代码。
def _next_actions(consistency: dict[str, Any]) -> list[str]:
    if not consistency["ok"]:
        return ["Stop automated work and inspect consistency_report before continuing."]
    return [
        "Read compact_context, work_state_snapshot, restore_refs, and self_check before answering.",
        "Compare goal, next_step, missing_fields, refs, and latest tests against the current task.",
        "Continue manually only after the restored state matches the intended task.",
    ]


# LLM: _artifact_ids_match detects accidental mixing of files from different compact apply attempts.
# 函数用途: 确认 metadata、apply bundle、restore refs、work state 和 self-check 的 apply_id/plan_id 一致。
def _artifact_ids_match(metadata: dict[str, Any], artifacts: dict[str, Any]) -> bool:
    expected = (metadata.get("apply_id"), metadata.get("plan_id"))
    records = [artifacts["apply_bundle"], artifacts["restore_refs"], artifacts["work_state"], artifacts["self_check"]]
    return all((record.get("apply_id"), record.get("plan_id")) == expected for record in records if record)


# LLM: _restore_refs_exist delegates to the work-state snapshot captured during compact apply.
# 函数用途: 判断原始 archive/snapshot/token 路径是否仍存在，缺失时阻断手动 resume。
def _restore_refs_exist(artifacts: dict[str, Any]) -> bool:
    work_state = artifacts["work_state"]
    restore = work_state.get("restore_refs", {}) if isinstance(work_state.get("restore_refs"), dict) else {}
    return bool(restore.get("all_source_paths_exist"))


# LLM: _work_state_missing_fields returns explicit unknown fields for manual review.
# 函数用途: 提取 work_state_snapshot 中记录的缺失字段，供一致性报告和上下文展示。
def _work_state_missing_fields(artifacts: dict[str, Any]) -> list[str]:
    work_state = artifacts["work_state"]
    value = work_state.get("missing_fields", [])
    return list(value) if isinstance(value, list) else []


# LLM: _source_paths extracts original fact-source paths from restore refs.
# 函数用途: 收集 restore refs 中的 archive、snapshot 和 token ledger 路径，供推荐读取。
def _source_paths(restore_refs: dict[str, Any]) -> list[str]:
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs, dict) else {}
    return [str(item.get("path", "")) for group in source_refs.values() for item in group if item.get("path")]


# LLM: _owner_payload records who owns this compact resume without changing current main-agent behavior.
# 函数用途: 预留 main/subagent/session compact 的 owner 字段，后续子代理自动会话压缩可直接复用。
def _owner_payload(options: MemoryCompactResumeOptions) -> dict[str, str]:
    return {"owner_type": options.owner_type, "owner_id": options.owner_id}


# LLM: _subagent_extension resolves task-local subagent refs while staying read-only.
# 函数用途: 为 subagent_run/subagent_session 返回 run workspace 引用，不改写 runner 或主 memory。
def _subagent_extension(workspace: Path, options: MemoryCompactResumeOptions) -> dict[str, Any]:
    return resolve_compact_subagent_owner(
        CompactSubagentOwnerRequest(
            workspace=workspace,
            owner_type=options.owner_type,
            owner_id=options.owner_id,
            resume_mode=options.resume_mode,
        )
    )


# LLM: _dedupe preserves read order while removing duplicate recommended paths.
# 函数用途: 对推荐读取路径去重，保持 compact 产物优先、源文件随后。
def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


__all__ = ["MemoryCompactResumeOptions", "build_memory_compact_resume"]
