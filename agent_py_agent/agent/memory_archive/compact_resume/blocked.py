
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..compact_action_guard import (
    CompactActionGuardOptions,
    CompactActionGuardRequest,
    build_compact_action_guard,
)
from ..schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_RESUME_SCHEMA = RuntimeMemorySchemaOptions("compact_resume")
COMPACT_RESUME_CONSISTENCY_SCHEMA = RuntimeMemorySchemaOptions("compact_resume_consistency_report")


@dataclass(frozen=True)
class BlockedCompactResumeRequest:
    workspace: Path
    apply_ref: str
    owner_type: str
    owner_id: str
    resume_mode: str
    metadata_path: Path
    status: str
    metadata_load_error: dict[str, object] | None = None


def build_blocked_compact_resume(request: BlockedCompactResumeRequest) -> dict[str, Any]:
    consistency = _blocked_consistency(request)
    action_guard = _blocked_action_guard(request, consistency)
    return {
        "version": COMPACT_RESUME_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_SCHEMA),
        "ok": False,
        "status": request.status,
        "mode": "resume_from_compact",
        "workspace_root": str(request.workspace),
        "apply_id": request.apply_ref,
        "plan_id": "",
        "owner": _owner_payload(request),
        "refs": {"metadata": str(request.metadata_path)},
        "metadata_load_error": dict(request.metadata_load_error or {}),
        "work_state": {},
        "consistency_report": consistency,
        "action_guard": action_guard,
        "handoff": {},
        "continue_packet": {},
        "fail_safe_checkpoints": [],
        "completion_prompt": {},
        "recommended_read_paths": [str(request.metadata_path)],
        "next_actions": ["Find a valid compact apply id or rerun memory-compact --apply."],
        "context_block": "",
    }


def _blocked_consistency(request: BlockedCompactResumeRequest) -> dict[str, Any]:
    return {
        "version": COMPACT_RESUME_CONSISTENCY_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_CONSISTENCY_SCHEMA),
        "ok": False,
        "status": request.status,
        "owner": _owner_payload(request),
        "apply_id": request.apply_ref,
        "plan_id": "",
        "checks": [{"name": "metadata_loaded", "ok": False, "severity": "hard"}],
        "metadata_load_error": dict(request.metadata_load_error or {}),
        "missing_fields": [],
    }


def _blocked_action_guard(request: BlockedCompactResumeRequest, consistency: dict[str, Any]) -> dict[str, Any]:
    return build_compact_action_guard(
        CompactActionGuardRequest(
            consistency_report=consistency,
            work_state={},
            refs={"metadata": str(request.metadata_path)},
            options=CompactActionGuardOptions(
                mode=request.resume_mode,
                owner_type=request.owner_type,
                owner_id=request.owner_id,
            ),
        )
    )


def _owner_payload(request: BlockedCompactResumeRequest) -> dict[str, str]:
    return {"owner_type": request.owner_type, "owner_id": request.owner_id}


__all__ = ["BlockedCompactResumeRequest", "build_blocked_compact_resume"]
