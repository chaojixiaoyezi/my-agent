from __future__ import annotations

"""Patch apply owner policy, validation, and recovery audit payloads."""

from dataclasses import dataclass
from typing import Any

from agent_py_agent.agent.subagents.reports import PatchApplyRecord
from agent_py_agent.agent.subagents.utils import _new_id


@dataclass(frozen=True)
class PatchBatchValidationParams:
    patches: list
    patch_specs: list
    blocked_count: int
    test_commands: list
    test_results: list
    apply: bool


def patch_owner_policy(task: Any, manager: Any, applier: str) -> dict[str, object]:
    permissions = getattr(task, "effective_permissions", {}) or {}
    return {
        "schema_version": "patch_owner_policy.v1",
        "run_id": str(getattr(task, "id", "") or ""),
        "task_owner": str(getattr(task, "owner", "") or ""),
        "final_owner": str(getattr(task, "final_owner", "") or ""),
        "applier": str(applier or ""),
        "manager_owner_id": str(getattr(manager, "owner_id", "") or ""),
        "allowed_write_roots": list(getattr(task, "allowed_write_roots", []) or []),
        "forbidden_write_roots": list(getattr(task, "forbidden_write_roots", []) or []),
        "locked_files": list(getattr(task, "locked_files", []) or []),
        "effective_permissions": dict(permissions) if isinstance(permissions, dict) else {},
        "owner_policy_snapshot": dict(getattr(manager, "owner_policy_snapshot", {}) or {}),
    }


def patch_batch_validation(params: PatchBatchValidationParams) -> dict[str, object]:
    failed_tests = [item for item in params.test_results if not item.get("ok")]
    return {
        "schema_version": "patch_batch_validation.v1",
        "requested_patch_count": len(params.patches),
        "validated_patch_count": len(params.patch_specs),
        "blocked_count": params.blocked_count,
        "test_command_count": len(params.test_commands),
        "failed_test_count": len(failed_tests),
        "apply_requested": bool(params.apply),
        "ready_for_apply": bool(params.patch_specs) and params.blocked_count == 0 and not failed_tests,
    }


def patch_failure_recovery(rollback_performed: bool, decision: str, message: str) -> dict[str, object]:
    return {
        "schema_version": "patch_failure_recovery.v1",
        "rollback_performed": bool(rollback_performed),
        "decision": decision,
        "message": message,
        "next_action": "inspect_rollback_and_retry_batch" if rollback_performed else "",
    }


@dataclass(frozen=True)
class PatchApplyRecordPayload:
    manager: Any
    task: Any
    params: Any
    now: float
    ok: bool
    decision: str
    message: str
    patch_specs: list
    blocked_count: int
    rollback_performed: bool
    test_commands: list
    test_results: list
    patch_entries: list
    evidence_paths: list
    applied_count: int


def build_patch_apply_record(payload: PatchApplyRecordPayload) -> PatchApplyRecord:
    params = payload.params
    return PatchApplyRecord(
        id=_new_id("patchapply"),
        run_id=payload.task.id,
        dry_run=not params.apply,
        applied=params.apply and payload.ok,
        ok=payload.ok,
        decision=payload.decision,
        message=payload.message,
        patch_count=len(params.patches),
        applied_count=payload.applied_count,
        blocked_count=payload.blocked_count,
        rollback_performed=payload.rollback_performed,
        applier=params.applier,
        note=params.note,
        evidence_paths=payload.evidence_paths,
        test_commands=payload.test_commands,
        test_results=payload.test_results,
        patches=payload.patch_entries,
        owner_policy=patch_owner_policy(payload.task, payload.manager, params.applier),
        batch_validation=patch_batch_validation(
            PatchBatchValidationParams(
                params.patches,
                payload.patch_specs,
                payload.blocked_count,
                payload.test_commands,
                payload.test_results,
                params.apply,
            )
        ),
        failure_recovery=patch_failure_recovery(payload.rollback_performed, payload.decision, payload.message),
        created_at=payload.now,
    )


__all__ = [
    "PatchBatchValidationParams",
    "PatchApplyRecordPayload",
    "build_patch_apply_record",
    "patch_batch_validation",
    "patch_failure_recovery",
    "patch_owner_policy",
]
