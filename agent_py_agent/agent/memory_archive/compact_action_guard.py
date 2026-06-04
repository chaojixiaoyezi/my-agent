
from __future__ import annotations

"""action guard for compact resume safety."""

from dataclasses import dataclass
from typing import Any

from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_ACTION_GUARD_SCHEMA = RuntimeMemorySchemaOptions("compact_action_guard")


@dataclass(frozen=True)
class CompactActionGuardOptions:
    mode: str = "manual"
    owner_type: str = "main_agent"
    owner_id: str = ""


@dataclass(frozen=True)
class CompactActionGuardRequest:
    consistency_report: dict[str, Any]
    work_state: dict[str, Any]
    refs: dict[str, Any]
    options: CompactActionGuardOptions


def build_compact_action_guard(request: CompactActionGuardRequest) -> dict[str, Any]:
    mode = _mode(request.options.mode)
    checks = _guard_checks(request, mode)
    hard_ok = all(item["ok"] for item in checks if item["severity"] == "hard")
    status = _guard_status(mode, hard_ok, _missing_fields(request.work_state))
    return {
        "version": COMPACT_ACTION_GUARD_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_ACTION_GUARD_SCHEMA),
        "ok": hard_ok,
        "mode": mode,
        "status": status,
        "allowed_to_continue": status == "allow_automated_continue",
        "allowed_next_action": _allowed_next_action(status),
        "automatic_tool_execution": "none",
        "owner": _owner_payload(request.options),
        "apply_id": request.consistency_report.get("apply_id", ""),
        "plan_id": request.consistency_report.get("plan_id", ""),
        "missing_fields": _missing_fields(request.work_state),
        "checks": checks,
    }


def _guard_checks(request: CompactActionGuardRequest, mode: str) -> list[dict[str, Any]]:
    work_state = request.work_state
    consistency = request.consistency_report
    missing = _missing_fields(work_state)
    return [
        {"name": "consistency_ok", "ok": bool(consistency.get("ok")), "severity": "hard"},
        {"name": "goal_present", "ok": bool(work_state.get("goal")), "severity": "hard"},
        {"name": "next_step_present", "ok": bool(work_state.get("next_step")), "severity": "soft"},
        {"name": "restore_refs_present", "ok": bool(request.refs.get("restore_refs")), "severity": "hard"},
        {"name": "self_check_present", "ok": bool(request.refs.get("post_compact_self_check")), "severity": "hard"},
        {"name": "optional_work_notes_present", "ok": not missing, "severity": "soft"},
        {"name": "manual_confirmation_required", "ok": mode == "manual", "severity": "soft"},
    ]


def _guard_status(mode: str, hard_ok: bool, missing_fields: list[str]) -> str:
    del missing_fields
    if not hard_ok:
        return "blocked_needs_human_review"
    if mode == "auto":
        return "allow_automated_continue"
    return "requires_user_confirmation"


def _allowed_next_action(status: str) -> str:
    if status == "allow_automated_continue":
        return "continue_after_guard"
    if status == "requires_user_confirmation":
        return "manual_review_then_continue"
    return "stop_and_request_review"


def _missing_fields(work_state: dict[str, Any]) -> list[str]:
    value = work_state.get("missing_fields", [])
    return [str(item) for item in value] if isinstance(value, list) else ["missing_fields"]


def _mode(value: str) -> str:
    return value if value in {"manual", "auto"} else "manual"


def _owner_payload(options: CompactActionGuardOptions) -> dict[str, str]:
    return {"owner_type": options.owner_type, "owner_id": options.owner_id}


__all__ = [
    "COMPACT_ACTION_GUARD_SCHEMA",
    "CompactActionGuardOptions",
    "CompactActionGuardRequest",
    "build_compact_action_guard",
]
