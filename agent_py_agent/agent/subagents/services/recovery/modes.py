from __future__ import annotations

"""Current structured recovery modes.

Recovery dispatch accepts only the explicit protocol modes below.
"""

from ....contracts.recovery_actions import RecoveryAction

NO_PROGRESS_LIMIT_REACHED = "no_progress_limit_reached"
LEADERSHIP_RECOVERY = "leadership_recovery"
TAKEOVER_FROM_CONTINUE_PACKET = "takeover_from_continue_packet"
TAKEOVER_FROM_CHECKPOINT = "takeover_from_checkpoint"
RERUN_FROM_CONTINUE_PACKET = "rerun_from_continue_packet"
RERUN_FROM_CHECKPOINT = "rerun_from_checkpoint"
CLOSED = "closed"
MANUAL_REVIEW_MISSING_REFS = "manual_review_missing_recovery_refs"

RERUN_MODES = frozenset({RERUN_FROM_CONTINUE_PACKET, RERUN_FROM_CHECKPOINT})
TAKEOVER_MODES = frozenset({TAKEOVER_FROM_CONTINUE_PACKET, TAKEOVER_FROM_CHECKPOINT})


def action_for_recovery_mode(mode: str) -> str:
    if mode == NO_PROGRESS_LIMIT_REACHED:
        return RecoveryAction.REPORT_BLOCKER.value
    if mode == LEADERSHIP_RECOVERY or is_takeover_mode(mode):
        return RecoveryAction.TAKEOVER.value
    if mode == RERUN_FROM_CONTINUE_PACKET:
        return RecoveryAction.RETRY.value
    if mode == RERUN_FROM_CHECKPOINT:
        return RecoveryAction.RECOVER_FROM_CHECKPOINT.value
    if mode == CLOSED:
        return RecoveryAction.NONE.value
    return RecoveryAction.MANUAL_REVIEW.value


def is_rerun_mode(mode: str) -> bool:
    return mode in RERUN_MODES


def is_takeover_mode(mode: str) -> bool:
    return mode in TAKEOVER_MODES


def mode_uses_continue_packet(mode: str) -> bool:
    return mode in {RERUN_FROM_CONTINUE_PACKET, TAKEOVER_FROM_CONTINUE_PACKET}


__all__ = [
    "CLOSED",
    "LEADERSHIP_RECOVERY",
    "MANUAL_REVIEW_MISSING_REFS",
    "NO_PROGRESS_LIMIT_REACHED",
    "RERUN_MODES",
    "RERUN_FROM_CHECKPOINT",
    "RERUN_FROM_CONTINUE_PACKET",
    "TAKEOVER_MODES",
    "TAKEOVER_FROM_CHECKPOINT",
    "TAKEOVER_FROM_CONTINUE_PACKET",
    "action_for_recovery_mode",
    "is_rerun_mode",
    "is_takeover_mode",
    "mode_uses_continue_packet",
]
