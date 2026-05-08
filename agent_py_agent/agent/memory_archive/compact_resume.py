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
from .compact_continue_packet import (
    CompactContinuePacketRequest,
    build_compact_continue_packet,
)
from .compact_resume_blocked import BlockedCompactResumeRequest, build_blocked_compact_resume
from .compact_resume_completion import (
    CompactCompletionPromptRequest,
    build_compact_completion_prompt,
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
from .compact_resume_paths import recommended_compact_resume_paths
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


# LLM: _HandoffBundleRequest keeps resume handoff construction separate from the top-level payload.
# 类用途: 汇总 handoff、completion prompt 和 context block 所需字段，避免恢复 payload 函数膨胀。
@dataclass(frozen=True)
class _HandoffBundleRequest:
    metadata: dict[str, Any]
    artifacts: dict[str, Any]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]
    recommended: list[str]
    next_actions: list[str]
    fail_safe_checkpoints: list[dict[str, Any]]


# LLM: _ContinuePacketBuildRequest keeps continue-packet inputs bundled at the resume boundary.
# 类用途: 汇总继续工作包构建所需字段，避免私有 helper 继续拉长参数列表。
@dataclass(frozen=True)
class _ContinuePacketBuildRequest:
    payload_request: _ResumePayloadBuildRequest
    recommended: list[str]
    next_actions: list[str]
    subagent_refs: dict[str, Any]
    handoff: dict[str, Any]


# LLM: build_memory_compact_resume is read-only; it never mutates archive, task, or subagent files.
# 函数用途: 从 apply_id 或 apply 产物路径读取恢复包，生成可人工注入的恢复上下文和一致性报告。
def build_memory_compact_resume(root: str | Path, options: MemoryCompactResumeOptions) -> dict[str, Any]:
    workspace = Path(root)
    metadata_path = resolve_compact_metadata_path(workspace, options.apply_ref)
    metadata = read_json_object(metadata_path)
    if not metadata:
        return build_blocked_compact_resume(
            BlockedCompactResumeRequest(
                workspace=workspace,
                apply_ref=options.apply_ref,
                owner_type=options.owner_type,
                owner_id=options.owner_id,
                resume_mode=options.resume_mode,
                metadata_path=metadata_path,
                status="blocked_missing_compact_metadata",
            )
        )
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
    recommended = recommended_compact_resume_paths(metadata, artifacts, fail_safe_checkpoints)
    next_actions = _next_actions(consistency)
    subagent_refs = _subagent_extension(request.workspace, request.options)
    handoff_bundle = _handoff_bundle(
        _HandoffBundleRequest(
            metadata=metadata,
            artifacts=artifacts,
            consistency=consistency,
            action_guard=action_guard,
            recommended=recommended,
            next_actions=next_actions,
            fail_safe_checkpoints=fail_safe_checkpoints,
        )
    )
    continue_packet = _continue_packet(
        _ContinuePacketBuildRequest(request, recommended, next_actions, subagent_refs, handoff_bundle["handoff"])
    )
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
        "handoff": handoff_bundle["handoff"],
        "continue_packet": continue_packet,
        "fail_safe_checkpoints": fail_safe_checkpoints,
        "completion_prompt": handoff_bundle["completion_prompt"],
        "recommended_read_paths": recommended,
        "next_actions": next_actions,
        "context_block": handoff_bundle["context_block"],
        "subagent_session_compact": subagent_refs,
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_SCHEMA),
    }


# LLM: _handoff_bundle builds the human/model handoff pieces from already validated compact artifacts.
# 函数用途: 生成 completion prompt、handoff 和可复制 context block；不读取或写入额外文件。
def _handoff_bundle(request: _HandoffBundleRequest) -> dict[str, Any]:
    completion_prompt = build_compact_completion_prompt(
        CompactCompletionPromptRequest(
            apply_id=str(request.metadata.get("apply_id", "")),
            plan_id=str(request.metadata.get("plan_id", "")),
            work_state=request.artifacts["work_state"],
        )
    )
    handoff = build_compact_resume_handoff(
        CompactResumeHandoffRequest(
            metadata=request.metadata,
            work_state=request.artifacts["work_state"],
            consistency=request.consistency,
            action_guard=request.action_guard,
            recommended_read_paths=request.recommended,
            next_actions=request.next_actions,
            fail_safe_checkpoints=request.fail_safe_checkpoints,
            completion_prompt=completion_prompt,
        )
    )
    return {
        "completion_prompt": completion_prompt,
        "handoff": handoff,
        "context_block": render_compact_resume_context_block(handoff),
    }


# LLM: _continue_packet freezes resume state for manual, semi-auto, and future auto callers.
# 函数用途: 生成继续工作包；只打包已读取的 compact resume 结果，不再读写任何文件。
def _continue_packet(request: _ContinuePacketBuildRequest) -> dict[str, Any]:
    payload_request = request.payload_request
    return build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata=payload_request.metadata,
            work_state=payload_request.artifacts["work_state"],
            consistency=payload_request.consistency,
            action_guard=payload_request.action_guard,
            handoff=request.handoff,
            recommended_read_paths=request.recommended,
            next_actions=request.next_actions,
            subagent_owner_refs=request.subagent_refs,
        )
    )


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


__all__ = ["MemoryCompactResumeOptions", "build_memory_compact_resume"]
