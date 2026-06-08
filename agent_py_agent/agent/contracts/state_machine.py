
from __future__ import annotations

import logging
from dataclasses import dataclass

from .error_taxonomy import error_contract
from .recovery import RecoveryAction, recovery_action_value

SCHEMA_VERSION = "state_machine.v1"
DISPATCHABLE_STATES = {"PLANNING", "PENDING"}
ACTIVE_STATES = {"RUNNING", "WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "WAITING_FOR_USER", "REPAIRING", "TAKING_OVER"}
TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED", "ABANDONED", "TIMEOUT", "CHANNEL_ERROR"}
WAITING_STATES = {"WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "WAITING_FOR_USER", "VERIFYING"}
BLOCKED_STATES = {"BLOCKED"}
REGISTERED_STATES = frozenset({
    *DISPATCHABLE_STATES,
    *ACTIVE_STATES,
    *WAITING_STATES,
    *BLOCKED_STATES,
    *TERMINAL_STATES,
})
REPAIRABLE_STATES = {"BLOCKED", "FAILED"}
VERIFIED_STATES = {"VERIFIED"}
VERIFICATION_STATES = {"UNVERIFIED", "VERIFIED", "FAILED"}
HEALTHY_CHANNEL_STATES = {"", "OK", "UNKNOWN"}
CHANNEL_STATES = {"", "OK", "UNKNOWN", "BROKEN"}
INVALID_STATUS_FALLBACK = "BLOCKED"
INVALID_VERIFICATION_FALLBACK = "UNVERIFIED"
INVALID_CHANNEL_FALLBACK = "BROKEN"
LOGGER = logging.getLogger(__name__)
_BLOCKED_REASON_BY_FAILURE = {
    "TOOL_UNAVAILABLE": "blocked_tool_unavailable",
    "WRITE_FORBIDDEN": "blocked_write_forbidden",
    "PATH_OUTSIDE_WORKSPACE": "blocked_path_outside_workspace",
}
_STATUS_REASON_BY_STATE = {
    "PLANNING": "planning",
    "PENDING": "pending",
    "RUNNING": "running",
    "WAITING_FOR_TOOL": "waiting_for_tool",
    "WAITING_FOR_CHILD": "waiting_for_child",
    "WAITING_FOR_USER": "waiting_for_user",
    "REPAIRING": "repairing",
    "TAKING_OVER": "taking_over",
    "VERIFYING": "verifying",
    "BLOCKED": "blocked",
    "DONE": "done",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "ABANDONED": "abandoned",
    "TIMEOUT": "timeout",
    "CHANNEL_ERROR": "channel_error",
}
_STATE_PROTOCOL_ERROR_CODES = {
    "STATE_STATUS_INVALID",
    "STATE_VERIFICATION_STATUS_INVALID",
    "STATE_CHANNEL_STATUS_INVALID",
}


@dataclass(frozen=True)
class RunStateFacts:
    status: str
    verification_status: str = ""
    channel_status: str = ""
    failure_type: str = ""
    attempts: int = 0
    max_attempts: int = 0
    has_progress: bool = True


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction | str
    allow_new_run: bool
    reason: str
    secondary_action: RecoveryAction | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", recovery_action_value(self.action))
        if self.secondary_action:
            object.__setattr__(self, "secondary_action", recovery_action_value(self.secondary_action))


def normalize_status(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "PLANNING"
    return text if text in REGISTERED_STATES else INVALID_STATUS_FALLBACK


def normalize_verification(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "UNVERIFIED"
    return text if text in VERIFICATION_STATES else INVALID_VERIFICATION_FALLBACK


def normalize_channel(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "UNKNOWN"
    return text if text in CHANNEL_STATES else INVALID_CHANNEL_FALLBACK


def can_dispatch(facts: RunStateFacts, *, force: bool = False) -> bool:
    status = normalize_status(facts.status)
    if status in DISPATCHABLE_STATES:
        return True
    if not force:
        return False
    return status in {"FAILED", "ABANDONED"} and _attempts_available(facts)


def can_closeout(facts: RunStateFacts) -> bool:
    return (
        normalize_status(facts.status) == "DONE"
        and normalize_verification(facts.verification_status) in VERIFIED_STATES
        and normalize_channel(facts.channel_status) in HEALTHY_CHANNEL_STATES
    )


def can_repair(facts: RunStateFacts) -> bool:
    status = normalize_status(facts.status)
    if normalize_channel(facts.channel_status) == "BROKEN":
        return _attempts_available(facts)
    if status == "BLOCKED":
        return _attempts_available(facts)
    if status in {"TIMEOUT", "CHANNEL_ERROR"}:
        return _attempts_available(facts)
    return status == "FAILED" and _attempts_available(facts)


def waiting_reason(facts: RunStateFacts) -> str:
    status = normalize_status(facts.status)
    verification = normalize_verification(facts.verification_status)
    failure = str(facts.failure_type or "").upper()
    if failure == "APPROVAL_REQUIRED":
        return "approval"
    if status == "WAITING_FOR_USER":
        return "user"
    if status == "WAITING_FOR_TOOL":
        return "tool"
    if status == "WAITING_FOR_CHILD":
        return "child"
    if status == "VERIFYING":
        return "verification"
    if status == "DONE" and verification not in VERIFIED_STATES:
        return "verification"
    if status == "RUNNING" and not facts.has_progress:
        return "local_progress"
    return "none"


def terminal_outcome(facts: RunStateFacts) -> str:
    status = normalize_status(facts.status)
    if can_closeout(facts):
        return "completed"
    if status == "TIMEOUT":
        return "timed_out"
    if status in {"CANCELLED", "ABANDONED"}:
        return "cancelled"
    if status in {"BLOCKED", "DONE"}:
        return "blocked"
    if status in {"FAILED", "CHANNEL_ERROR"} or normalize_channel(facts.channel_status) == "BROKEN":
        return "failed"
    return "active"


def lifecycle_phase(facts: RunStateFacts) -> str:
    status = normalize_status(facts.status)
    channel = normalize_channel(facts.channel_status)
    reason = waiting_reason(facts)
    if channel == "BROKEN":
        return "BLOCKED"
    if status in {"TIMEOUT", "CHANNEL_ERROR"}:
        return "BLOCKED"
    if reason in {"approval", "user"}:
        return "WAITING_FOR_USER"
    if reason == "acceptance":
        return "VERIFYING"
    if reason == "local_progress":
        return "WAITING_FOR_LOCAL_PROGRESS"
    if reason == "tool":
        return "WAITING_FOR_TOOL"
    if reason == "child":
        return "WAITING_FOR_CHILD"
    if status in {"BLOCKED", "FAILED", "CANCELLED", "ABANDONED"}:
        return "BLOCKED"
    if status in {"PLANNING", "PENDING", "RUNNING"}:
        return status
    return "DONE" if can_closeout(facts) else status


def recovery_decision(facts: RunStateFacts) -> RecoveryDecision:
    failure = str(facts.failure_type or "").upper()
    if _status_protocol_error(facts.status):
        return RecoveryDecision(RecoveryAction.MANUAL_REVIEW, False, "invalid_state_status_protocol")
    if _verification_protocol_error(facts.verification_status):
        return RecoveryDecision(RecoveryAction.MANUAL_REVIEW, False, "invalid_verification_status_protocol")
    if _channel_protocol_error(facts.channel_status):
        return RecoveryDecision(RecoveryAction.MANUAL_REVIEW, False, "invalid_channel_status_protocol")
    if failure in _STATE_PROTOCOL_ERROR_CODES:
        return RecoveryDecision(RecoveryAction.MANUAL_REVIEW, False, f"invalid_{failure.lower()}")
    status = normalize_status(facts.status)
    channel = normalize_channel(facts.channel_status)
    if can_closeout(facts):
        return RecoveryDecision(RecoveryAction.CLOSEOUT, False, "done_verified")
    if channel == "BROKEN":
        return RecoveryDecision(RecoveryAction.REPAIR_CHANNEL, False, "channel_broken")
    if status in {"TIMEOUT", "CHANNEL_ERROR"} and can_repair(facts):
        return RecoveryDecision(RecoveryAction.REPAIR, False, "repairable_failure")
    if waiting_reason(facts) == "acceptance":
        return RecoveryDecision(RecoveryAction.WAIT_FOR_ACCEPTANCE, False, "done_unverified")
    if failure in {"NO_PROGRESS", "NO_PROGRESS_FUSE"}:
        return RecoveryDecision(RecoveryAction.CHANGE_STRATEGY, False, "no_progress", RecoveryAction.STOP)
    if failure == "APPROVAL_REQUIRED":
        return RecoveryDecision(RecoveryAction.REQUEST_APPROVAL, False, "approval_required", RecoveryAction.STOP)
    if status in DISPATCHABLE_STATES:
        return RecoveryDecision(RecoveryAction.DISPATCH, False, "not_started")
    if status == "RUNNING" and not facts.has_progress:
        return RecoveryDecision(RecoveryAction.WAIT_FOR_LOCAL_PROGRESS, False, "running_without_local_progress")
    if status in ACTIVE_STATES:
        return RecoveryDecision(RecoveryAction.WAIT, False, "already_active")
    if status == "BLOCKED" and failure in {"TOOL_UNAVAILABLE", "WRITE_FORBIDDEN", "PATH_OUTSIDE_WORKSPACE"}:
        if can_repair(facts):
            return RecoveryDecision(RecoveryAction.REQUEST_CAPABILITY, False, _BLOCKED_REASON_BY_FAILURE[failure])
        return RecoveryDecision(RecoveryAction.TAKEOVER, True, "attempts_exhausted", RecoveryAction.STOP)
    if status == "BLOCKED" and _structured_repair_action(failure):
        if can_repair(facts):
            return RecoveryDecision(_structured_repair_action(failure), False, _recovery_reason("blocked", failure))
        return RecoveryDecision(RecoveryAction.TAKEOVER, True, "attempts_exhausted", RecoveryAction.STOP)
    if can_repair(facts):
        return RecoveryDecision(RecoveryAction.REPAIR, False, "repairable_failure")
    if status in {"BLOCKED", "FAILED"}:
        return RecoveryDecision(RecoveryAction.TAKEOVER, True, "attempts_exhausted", RecoveryAction.STOP)
    LOGGER.warning("unhandled recovery state: status=%s failure=%s", status, failure)
    return RecoveryDecision(RecoveryAction.MANUAL_REVIEW, False, _recovery_reason("unhandled_state", status))


def run_state_snapshot_from_task(task: object) -> dict[str, object]:
    raw_status = str(getattr(task, "status", "") or "").strip()
    raw_verification = str(getattr(task, "verification_status", "") or "").strip()
    raw_channel = str(getattr(task, "channel_status", "") or "").strip()
    status_error = _status_protocol_error(raw_status)
    verification_error = _verification_protocol_error(raw_verification)
    channel_error = _channel_protocol_error(raw_channel)
    failure_type = _failure_type_from_task(task)
    if status_error:
        failure_type = status_error
    elif verification_error:
        failure_type = verification_error
    elif channel_error:
        failure_type = channel_error
    facts = RunStateFacts(
        status=normalize_status(raw_status),
        verification_status=normalize_verification(raw_verification),
        channel_status=normalize_channel(raw_channel),
        failure_type=failure_type,
        attempts=_int_attr(task, "runner_attempts"),
        max_attempts=_int_attr(task, "runner_max_attempts"),
        has_progress=bool(getattr(task, "has_progress", True)),
    )
    decision = recovery_decision(facts)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(getattr(task, "id", "") or ""),
        "status": normalize_status(facts.status),
        "verification_status": normalize_verification(facts.verification_status),
        "channel_status": normalize_channel(facts.channel_status),
        "raw_status": raw_status,
        "raw_verification_status": raw_verification,
        "raw_channel_status": raw_channel,
        "status_protocol_error": status_error,
        "verification_protocol_error": verification_error,
        "channel_protocol_error": channel_error,
        "lifecycle_phase": lifecycle_phase(facts),
        "waiting_reason": waiting_reason(facts),
        "terminal_outcome": terminal_outcome(facts),
        "failure_type": error_contract(facts.failure_type or "UNKNOWN_ERROR").code,
        "attempts": facts.attempts,
        "max_attempts": facts.max_attempts,
        "can_dispatch": can_dispatch(facts),
        "can_closeout": can_closeout(facts),
        "can_repair": can_repair(facts),
        "recovery_decision": {
            "action": decision.action,
            "allow_new_run": decision.allow_new_run,
            "reason": decision.reason,
            "secondary_action": decision.secondary_action or "",
        },
    }


def _failure_type_from_task(task: object) -> str:
    raw = str(getattr(task, "failure_type", "") or "").strip()
    if raw:
        return error_contract(raw).code
    return "UNKNOWN_ERROR"


def _status_protocol_error(value: object) -> str:
    text = str(value or "").strip()
    return "" if not text or text in REGISTERED_STATES else "STATE_STATUS_INVALID"


def _verification_protocol_error(value: object) -> str:
    text = str(value or "").strip()
    return "" if not text or text in VERIFICATION_STATES else "STATE_VERIFICATION_STATUS_INVALID"


def _channel_protocol_error(value: object) -> str:
    text = str(value or "").strip()
    return "" if not text or text in CHANNEL_STATES else "STATE_CHANNEL_STATUS_INVALID"


def _int_attr(task: object, name: str) -> int:
    try:
        return int(getattr(task, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _attempts_available(facts: RunStateFacts) -> bool:
    return facts.max_attempts <= 0 or facts.attempts < facts.max_attempts


def _structured_repair_action(failure: str) -> str:
    contract = error_contract(failure)
    if contract.code == "UNKNOWN_ERROR":
        return ""
    if contract.category in {"artifact", "evidence", "tool", "path", "acceptance", "compact", "model", "orchestration"}:
        return contract.recommended_action
    return ""


def _recovery_reason(prefix: str, code: str) -> str:
    contract_code = error_contract(code).code
    if contract_code != "UNKNOWN_ERROR":
        return f"{prefix}_{contract_code.lower()}"
    return f"{prefix}_{_STATUS_REASON_BY_STATE.get(code, 'unknown')}"


__all__ = [
    "SCHEMA_VERSION",
    "RunStateFacts",
    "RecoveryDecision",
    "can_closeout",
    "can_dispatch",
    "can_repair",
    "lifecycle_phase",
    "normalize_channel",
    "normalize_status",
    "normalize_verification",
    "recovery_decision",
    "run_state_snapshot_from_task",
    "terminal_outcome",
    "waiting_reason",
]
