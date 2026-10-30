# LLM: 模式为精确恢复协议；坏恢复账走独立人工复核，不冒充缺refs、已知无进展或可自动续派。
# 模块用途: 定义子代理恢复模式和原动作映射，不授予执行权限。
"""Structured recovery mode enum.

Recovery dispatch accepts only the explicit protocol modes below.
String aliases and natural-language status text are NOT valid recovery modes.
"""

from __future__ import annotations

from enum import Enum

from ....contracts.recovery import RecoveryAction


# LLM: 新增unknown-history只投影到原MANUAL_REVIEW动作，自动重跑/接管集合不扩张。
# 类用途: 区分宿主可验证的恢复模式及不能自动执行的复核原因。
class RecoveryMode(str, Enum):
    """Canonical recovery mode for subagent failure/stall handling.

    Each value is a machine-readable key. Recovery dispatch does not parse
    natural-language descriptions into modes.
    """

    NO_PROGRESS_LIMIT_REACHED = "no_progress_limit_reached"
    LEADERSHIP_RECOVERY = "leadership_recovery"
    TAKEOVER_FROM_CHECKPOINT = "takeover_from_checkpoint"
    RERUN_FROM_CHECKPOINT = "rerun_from_checkpoint"
    CLOSED = "closed"
    MANUAL_REVIEW_MISSING_REFS = "manual_review_missing_recovery_refs"
    MANUAL_REVIEW_UNKNOWN_HISTORY = "manual_review_unknown_recovery_history"

    def to_action(self) -> str:
        """Return the RecoveryAction value for this mode."""
        if self is RecoveryMode.NO_PROGRESS_LIMIT_REACHED:
            return RecoveryAction.REPORT_BLOCKER.value
        if self is RecoveryMode.LEADERSHIP_RECOVERY or self.is_takeover():
            return RecoveryAction.TAKEOVER.value
        if self is RecoveryMode.RERUN_FROM_CHECKPOINT:
            return RecoveryAction.RECOVER_FROM_CHECKPOINT.value
        if self is RecoveryMode.CLOSED:
            return RecoveryAction.NONE.value
        return RecoveryAction.MANUAL_REVIEW.value

    def is_rerun(self) -> bool:
        return self in RERUN_MODES

    def is_takeover(self) -> bool:
        return self in TAKEOVER_MODES

RERUN_MODES = frozenset({RecoveryMode.RERUN_FROM_CHECKPOINT})
TAKEOVER_MODES = frozenset({RecoveryMode.TAKEOVER_FROM_CHECKPOINT})


def action_for_recovery_mode(mode: RecoveryMode) -> str:
    """Return the RecoveryAction for a recovery mode value."""
    return mode.to_action()


def is_rerun_mode(mode: RecoveryMode) -> bool:
    return mode.is_rerun()


def is_takeover_mode(mode: RecoveryMode) -> bool:
    return mode.is_takeover()


def recovery_mode_or_manual(mode: RecoveryMode | None) -> RecoveryMode:
    """Return the structured mode, or manual review when no mode exists."""
    if isinstance(mode, RecoveryMode):
        return mode
    return RecoveryMode.MANUAL_REVIEW_MISSING_REFS


def recovery_mode_from_protocol_value(value: object) -> RecoveryMode:
    """Parse an exact wire value from serialized recovery strategy payloads."""
    if isinstance(value, RecoveryMode):
        return value
    try:
        return RecoveryMode(str(value or "").strip())
    except ValueError:
        return RecoveryMode.MANUAL_REVIEW_MISSING_REFS


__all__ = [
    "RERUN_MODES",
    "RecoveryMode",
    "TAKEOVER_MODES",
    "action_for_recovery_mode",
    "is_rerun_mode",
    "is_takeover_mode",
    "recovery_mode_from_protocol_value",
    "recovery_mode_or_manual",
]
