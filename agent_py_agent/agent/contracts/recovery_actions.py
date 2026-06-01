# LLM: Recovery action constants keep state-machine and packet recommendations on one vocabulary.
# 模块用途: 统一恢复动作码，避免状态机和恢复包各自发明一套 recommended_action。

from __future__ import annotations

from enum import Enum


class RecoveryAction(str, Enum):
    """Canonical recovery actions shared by state machine and recovery packets."""

    CLOSEOUT = "closeout"
    DISPATCH = "dispatch"
    WAIT_FOR_ACCEPTANCE = "wait_for_acceptance"
    REQUEST_APPROVAL_OR_STOP = "request_approval_or_stop"
    CHANGE_STRATEGY_OR_STOP = "change_strategy_or_stop"
    WAIT_FOR_LOCAL_PROGRESS = "wait_for_local_progress"
    WAIT_OR_OBSERVE = "wait_or_observe"
    REPAIR_OR_PROBE_CHANNEL = "repair_or_probe_channel"
    REPAIR_OR_REQUEST_CAPABILITY = "repair_or_request_capability"
    REPAIR = "repair"
    TAKEOVER_OR_STOP = "takeover_or_stop"
    MANUAL_REVIEW = "manual_review"
    NONE = "none"
    RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT = "resume_same_case_after_idle_timeout"
    RESUME_SAME_CASE_AFTER_TIMEOUT = "resume_same_case_after_timeout"
    REPAIR_THEN_RESUME_SAME_CASE = "repair_then_resume_same_case"


RECOVERY_ACTIONS = tuple(action.value for action in RecoveryAction)

ACTION_CLOSEOUT = RecoveryAction.CLOSEOUT.value
ACTION_DISPATCH = RecoveryAction.DISPATCH.value
ACTION_WAIT_FOR_ACCEPTANCE = RecoveryAction.WAIT_FOR_ACCEPTANCE.value
ACTION_REQUEST_APPROVAL_OR_STOP = RecoveryAction.REQUEST_APPROVAL_OR_STOP.value
ACTION_CHANGE_STRATEGY_OR_STOP = RecoveryAction.CHANGE_STRATEGY_OR_STOP.value
ACTION_WAIT_FOR_LOCAL_PROGRESS = RecoveryAction.WAIT_FOR_LOCAL_PROGRESS.value
ACTION_WAIT_OR_OBSERVE = RecoveryAction.WAIT_OR_OBSERVE.value
ACTION_REPAIR_OR_PROBE_CHANNEL = RecoveryAction.REPAIR_OR_PROBE_CHANNEL.value
ACTION_REPAIR_OR_REQUEST_CAPABILITY = RecoveryAction.REPAIR_OR_REQUEST_CAPABILITY.value
ACTION_REPAIR = RecoveryAction.REPAIR.value
ACTION_TAKEOVER_OR_STOP = RecoveryAction.TAKEOVER_OR_STOP.value
ACTION_MANUAL_REVIEW = RecoveryAction.MANUAL_REVIEW.value
ACTION_NONE = RecoveryAction.NONE.value
ACTION_RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT = RecoveryAction.RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT.value
ACTION_RESUME_SAME_CASE_AFTER_TIMEOUT = RecoveryAction.RESUME_SAME_CASE_AFTER_TIMEOUT.value
ACTION_REPAIR_THEN_RESUME_SAME_CASE = RecoveryAction.REPAIR_THEN_RESUME_SAME_CASE.value


def recovery_action_value(action: RecoveryAction | str) -> str:
    """Return a canonical action value or raise for unknown recovery actions."""
    if isinstance(action, RecoveryAction):
        return action.value
    value = str(action or "").strip()
    if value in known_recovery_action_values():
        return value
    raise ValueError(f"unknown recovery action: {value!r}")


def known_recovery_action_values() -> tuple[str, ...]:
    """Return coarse state-machine actions plus taxonomy repair actions."""
    values = {*RECOVERY_ACTIONS}
    values.update(_error_taxonomy_recovery_actions())
    return tuple(sorted(values))


def _error_taxonomy_recovery_actions() -> set[str]:
    from .error_taxonomy import ERROR_CONTRACTS

    return {
        str(contract.recommended_action or "").strip()
        for contract in ERROR_CONTRACTS.values()
        if str(contract.recommended_action or "").strip()
    }

__all__ = [
    "ACTION_CHANGE_STRATEGY_OR_STOP",
    "ACTION_CLOSEOUT",
    "ACTION_DISPATCH",
    "ACTION_MANUAL_REVIEW",
    "ACTION_NONE",
    "ACTION_REPAIR",
    "ACTION_REPAIR_OR_PROBE_CHANNEL",
    "ACTION_REPAIR_OR_REQUEST_CAPABILITY",
    "ACTION_REPAIR_THEN_RESUME_SAME_CASE",
    "ACTION_REQUEST_APPROVAL_OR_STOP",
    "ACTION_RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT",
    "ACTION_RESUME_SAME_CASE_AFTER_TIMEOUT",
    "ACTION_TAKEOVER_OR_STOP",
    "ACTION_WAIT_FOR_ACCEPTANCE",
    "ACTION_WAIT_FOR_LOCAL_PROGRESS",
    "ACTION_WAIT_OR_OBSERVE",
    "RECOVERY_ACTIONS",
    "RecoveryAction",
    "known_recovery_action_values",
    "recovery_action_value",
]
