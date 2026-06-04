
from __future__ import annotations

"""manual resume support for memory-compact --apply artifacts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..compact_action_guard import (
    CompactActionGuardOptions,
    CompactActionGuardRequest,
    build_compact_action_guard,
)
from ..compact_context_bundle import compact_context_bundle_summary
from ..compact_gate_bridge import evaluate_post_compaction_state
from ..compact_subagent_owner import (
    CompactSubagentOwnerRequest,
    resolve_compact_subagent_owner,
)
from ..schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)
from .blocked import BlockedCompactResumeRequest, build_blocked_compact_resume
from .failsafe import collect_fail_safe_checkpoint_report
from .io import (
    read_compact_apply_artifacts,
    read_compact_apply_artifacts_report,
    read_json_object,
    read_json_object_report,
    resolve_compact_metadata_path,
)
from .paths import recommended_compact_resume_paths
from .payloads import (
    CompactResumePayloadPartsRequest,
    build_compact_resume_payload_parts,
)

COMPACT_RESUME_SCHEMA = RuntimeMemorySchemaOptions("compact_resume")
COMPACT_RESUME_CONSISTENCY_SCHEMA = RuntimeMemorySchemaOptions("compact_resume_consistency_report")



@dataclass(frozen=True)
class MemoryCompactResumeOptions:
    apply_ref: str
    owner_type: str = "main_agent"
    owner_id: str = ""
    resume_mode: str = "manual"
    subagent_workspace: str | Path = ""


@dataclass(frozen=True)
class _ResumePayloadBuildRequest:
    workspace: Path
    options: MemoryCompactResumeOptions
    metadata: dict[str, Any]
    artifacts: dict[str, Any]
    artifact_load_errors: list[dict[str, object]]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]


def build_memory_compact_resume(root: str | Path, options: MemoryCompactResumeOptions) -> dict[str, Any]:
    workspace = Path(root)
    metadata_path = resolve_compact_metadata_path(workspace, options.apply_ref)
    metadata_report = read_json_object_report(metadata_path, context="compact_resume.metadata")
    metadata = metadata_report.payload
    if not metadata:
        status = "blocked_compact_metadata_load_error" if metadata_report.load_error else "blocked_missing_compact_metadata"
        return build_blocked_compact_resume(
            BlockedCompactResumeRequest(
                workspace=workspace,
                apply_ref=options.apply_ref,
                owner_type=options.owner_type,
                owner_id=options.owner_id,
                resume_mode=options.resume_mode,
                metadata_path=metadata_path,
                status=status,
                metadata_load_error=metadata_report.load_error,
            )
        )
    artifact_report = read_compact_apply_artifacts_report(metadata)
    artifacts = artifact_report.artifacts
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
    return _resume_payload(
        _ResumePayloadBuildRequest(workspace, options, metadata, artifacts, artifact_report.load_errors, consistency, action_guard)
    )


def _consistency_report(
    metadata: dict[str, Any], artifacts: dict[str, Any], options: MemoryCompactResumeOptions
) -> dict[str, Any]:
    compaction_gate = evaluate_post_compaction_state(metadata, artifacts)
    checks = _consistency_checks(metadata, artifacts, compaction_gate)
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
        "compaction_gate": compaction_gate,
        "artifact_load_errors": [],
    }


def _consistency_checks(
    metadata: dict[str, Any], artifacts: dict[str, Any], compaction_gate: dict[str, Any]
) -> list[dict[str, Any]]:
    work_state = artifacts["work_state"]
    gate_present = bool(compaction_gate.get("present"))
    gate_allowed = bool(compaction_gate.get("post", {}).get("allowed", True))
    return [
        {"name": "metadata_loaded", "ok": bool(metadata), "severity": "hard"},
        {"name": "apply_bundle_loaded", "ok": bool(artifacts["apply_bundle"]), "severity": "hard"},
        {"name": "restore_refs_loaded", "ok": bool(artifacts["restore_refs"]), "severity": "hard"},
        {"name": "work_state_snapshot_loaded", "ok": bool(work_state), "severity": "hard"},
        {"name": "compaction_state_loaded", "ok": bool(artifacts.get("compaction_state")), "severity": "soft"},
        {"name": "handoff_summary_loaded", "ok": bool(artifacts.get("handoff_summary")), "severity": "soft"},
        {"name": "self_check_loaded", "ok": bool(artifacts["self_check"]), "severity": "hard"},
        {"name": "self_check_ok", "ok": bool(artifacts["self_check"].get("ok")), "severity": "hard"},
        {"name": "apply_ids_consistent", "ok": _artifact_ids_match(metadata, artifacts), "severity": "hard"},
        {"name": "restore_refs_exist", "ok": _restore_refs_exist(artifacts), "severity": "hard"},
        {"name": "compact_context_loaded", "ok": bool(artifacts["compact_context"]), "severity": "hard"},
        {"name": "compaction_gate_present", "ok": gate_present, "severity": "soft"},
        {"name": "compaction_gate_ok", "ok": gate_allowed, "severity": "hard" if gate_present else "soft"},
        {"name": "goal_present", "ok": bool(work_state.get("goal")), "severity": "soft"},
        {"name": "next_step_present", "ok": bool(work_state.get("next_step")), "severity": "soft"},
        {"name": "missing_fields_recorded", "ok": isinstance(work_state.get("missing_fields"), list), "severity": "soft"},
    ]


def _resume_payload(request: _ResumePayloadBuildRequest) -> dict[str, Any]:
    metadata = request.metadata
    artifacts = request.artifacts
    consistency = request.consistency
    action_guard = request.action_guard
    parts = _resume_payload_parts(request)
    return {
        "version": COMPACT_RESUME_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_SCHEMA),
        "ok": consistency["ok"],
        "mode": "resume_from_compact",
        "workspace_root": str(request.workspace),
        "apply_id": metadata.get("apply_id", ""),
        "plan_id": metadata.get("plan_id", ""),
        "lineage": dict(metadata.get("lineage", {}) if isinstance(metadata.get("lineage"), dict) else {}),
        "owner": _owner_payload(request.options),
        "refs": metadata.get("refs", {}),
        "work_state": artifacts["work_state"],
        "compaction_state": artifacts.get("compaction_state", {}),
        "handoff_summary": _handoff_summary_payload(artifacts),
        "consistency_report": consistency,
        "action_guard": action_guard,
        "compaction_gate": consistency.get("compaction_gate", {}),
        "main_context_bundle": parts["main_context_bundle"],
        "handoff": parts["handoff"],
        "continue_packet": parts["continue_packet"],
        "artifact_read_hints": parts["handoff"].get("artifact_read_hints", []),
        "fail_safe_checkpoints": parts["fail_safe_checkpoints"],
        "fail_safe_checkpoint_load_errors": parts["fail_safe_checkpoint_load_errors"],
        "artifact_load_errors": list(request.artifact_load_errors),
        "completion_prompt": parts["completion_prompt"],
        "recommended_read_paths": parts["recommended"],
        "next_actions": parts["next_actions"],
        "context_block": parts["context_block"],
        "subagent_session_compact": parts["subagent_refs"],
    }


def _resume_payload_parts(request: _ResumePayloadBuildRequest) -> dict[str, Any]:
    metadata = request.metadata
    artifacts = request.artifacts
    consistency = request.consistency
    action_guard = request.action_guard
    fail_safe_report = collect_fail_safe_checkpoint_report(artifacts["restore_refs"])
    fail_safe_checkpoints = fail_safe_report.checkpoints
    recommended = recommended_compact_resume_paths(metadata, artifacts, fail_safe_checkpoints)
    next_actions = _next_actions(consistency)
    subagent_refs = _subagent_extension(request.workspace, request.options)
    main_context_bundle = compact_context_bundle_summary(metadata.get("main_context_bundle", {}))
    payload_parts = build_compact_resume_payload_parts(
        CompactResumePayloadPartsRequest(
            metadata=metadata,
            artifacts=artifacts,
            consistency=consistency,
            action_guard=action_guard,
            recommended=recommended,
            next_actions=next_actions,
            subagent_refs=subagent_refs,
            fail_safe_checkpoints=fail_safe_checkpoints,
            fail_safe_checkpoint_load_errors=fail_safe_report.load_errors,
            artifact_load_errors=request.artifact_load_errors,
            main_context_bundle=main_context_bundle,
            compaction_state=artifacts.get("compaction_state", {}),
            handoff_summary=str(artifacts.get("handoff_summary", "") or ""),
        )
    )
    return {
        "fail_safe_checkpoints": fail_safe_checkpoints,
        "fail_safe_checkpoint_load_errors": fail_safe_report.load_errors,
        "artifact_load_errors": request.artifact_load_errors,
        "recommended": recommended,
        "next_actions": next_actions,
        "subagent_refs": subagent_refs,
        "main_context_bundle": main_context_bundle,
        "handoff": payload_parts.handoff,
        "completion_prompt": payload_parts.completion_prompt,
        "context_block": payload_parts.context_block,
        "continue_packet": payload_parts.continue_packet,
    }


def _handoff_summary_payload(artifacts: dict[str, Any]) -> dict[str, Any]:
    state = artifacts.get("compaction_state", {})
    state = state if isinstance(state, dict) else {}
    return {
        "ref": str(state.get("handoff_summary_ref") or ""),
        "previous_ref": str(state.get("previous_handoff_summary_ref") or ""),
        "text": str(artifacts.get("handoff_summary", "") or ""),
    }


# 新手说明: 这里要先让模型按 continue_packet.resume_focus 接着干活；compact 文件是备用证据，不是每次恢复都先重读的清单。
def _next_actions(consistency: dict[str, Any]) -> list[str]:
    if not consistency["ok"]:
        return ["Stop automated work and inspect consistency_report before continuing."]
    return [
        "Continue from continue_packet.resume_focus.next_action first.",
        "Use continue_packet.work_state_snapshot.captured_refs to avoid repeating finished reads, writes, and dispatches.",
        "Read compact refs only when the next action lacks facts, needs verification, or source refs look broken.",
    ]


def _artifact_ids_match(metadata: dict[str, Any], artifacts: dict[str, Any]) -> bool:
    expected = (metadata.get("apply_id"), metadata.get("plan_id"))
    records = [artifacts["apply_bundle"], artifacts["restore_refs"], artifacts["work_state"], artifacts["self_check"]]
    return all((record.get("apply_id"), record.get("plan_id")) == expected for record in records if record)


def _restore_refs_exist(artifacts: dict[str, Any]) -> bool:
    work_state = artifacts["work_state"]
    restore = work_state.get("restore_refs", {}) if isinstance(work_state.get("restore_refs"), dict) else {}
    return bool(restore.get("all_source_paths_exist"))


def _work_state_missing_fields(artifacts: dict[str, Any]) -> list[str]:
    work_state = artifacts["work_state"]
    value = work_state.get("missing_fields", [])
    return list(value) if isinstance(value, list) else []


def _owner_payload(options: MemoryCompactResumeOptions) -> dict[str, str]:
    return {"owner_type": options.owner_type, "owner_id": options.owner_id}


def _subagent_extension(workspace: Path, options: MemoryCompactResumeOptions) -> dict[str, Any]:
    return resolve_compact_subagent_owner(
        CompactSubagentOwnerRequest(
            workspace=workspace,
            owner_type=options.owner_type,
            owner_id=options.owner_id,
            resume_mode=options.resume_mode,
            subagent_workspace=_subagent_workspace_path(workspace, options.subagent_workspace),
        )
    )


def _subagent_workspace_path(workspace: Path, value: str | Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else workspace / path


__all__ = ["MemoryCompactResumeOptions", "build_memory_compact_resume"]
