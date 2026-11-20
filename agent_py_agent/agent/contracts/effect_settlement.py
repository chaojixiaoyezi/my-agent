"""按结构化副作用回执裁决结算，不授予重放权限。"""

from __future__ import annotations

SETTLEMENT_APPLIED = "applied"
SETTLEMENT_NOT_STARTED = "not_started"
SETTLEMENT_FAILED = "failed"
SETTLEMENT_PARTIAL = "partial"
SETTLEMENT_UNKNOWN = "unknown"

_TERMINAL_SUCCESS = frozenset({"done", "succeeded"})
_EXPLICIT_SETTLEMENT = {
    SETTLEMENT_UNKNOWN: SETTLEMENT_UNKNOWN,
    SETTLEMENT_PARTIAL: SETTLEMENT_PARTIAL,
    SETTLEMENT_NOT_STARTED: SETTLEMENT_NOT_STARTED,
    SETTLEMENT_FAILED: SETTLEMENT_FAILED,
    SETTLEMENT_APPLIED: SETTLEMENT_APPLIED,
    "confirmed": SETTLEMENT_APPLIED,
}


def classify_effect_settlement(
    handler_executed: bool | None, effect_outcome: str | None, status: str, /, *,
    unknown_reason: str = "",
) -> str:
    """显式回执优先，旧失败行只在读取时解释。"""
    explicit = _EXPLICIT_SETTLEMENT.get(effect_outcome) if effect_outcome is not None else None
    if explicit is not None:
        return explicit
    normalized_status = status.lower()
    if handler_executed is True:
        return SETTLEMENT_APPLIED if normalized_status in _TERMINAL_SUCCESS else SETTLEMENT_UNKNOWN
    if effect_outcome:
        return SETTLEMENT_UNKNOWN
    if normalized_status in _TERMINAL_SUCCESS:
        return SETTLEMENT_APPLIED
    if normalized_status == "failed":
        if normalize_legacy_operation_status(status, unknown_reason) == "unknown":
            return SETTLEMENT_UNKNOWN
        return SETTLEMENT_NOT_STARTED
    return SETTLEMENT_UNKNOWN


def operation_status_for_settlement(settlement: str, /, *, ok: bool) -> str:
    """把结算事实映射到工具操作账本状态，原事实仍保留在 effect_outcome。"""
    if ok:
        return "succeeded"
    if settlement in {SETTLEMENT_FAILED, SETTLEMENT_NOT_STARTED}:
        return "failed"
    return "unknown"


def normalize_legacy_operation_status(status: str, unknown_reason: str) -> str:
    """历史协调器的执行后写入错误在读口显示为未知。"""
    if status.lower() == "failed" and unknown_reason.startswith("coordinator_post_execution_error"):
        return "unknown"
    return status
