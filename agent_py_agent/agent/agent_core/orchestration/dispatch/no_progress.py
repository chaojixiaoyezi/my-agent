# LLM: Host dispatch-loop progress detection only. This module must not emit
# model-callable recovery or polling instructions.
# 模块用途: 判断内部 dispatcher 是否连续无进展，供宿主有界停止循环。
from __future__ import annotations

from dataclasses import dataclass


# LLM: Tracker compares typed dispatch records and owns no recovery side effects.
# 类用途: 连续两轮结构化签名不变时通知宿主停止空转。
@dataclass
class DispatchNoProgressTracker:
    previous_signature: tuple | None = None
    repeated_rounds: int = 0

    # LLM: None means material progress and resets the streak; identical
    # record-only signatures increment it without reading model prose.
    # 函数用途: 更新无进展计数，并在连续两轮相同时返回停止信号。
    def should_stop(self, dispatch_report) -> bool:
        signature = _dispatch_no_progress_signature(dispatch_report)
        if signature is None:
            self.previous_signature = None
            self.repeated_rounds = 0
            return False
        if signature == self.previous_signature:
            self.repeated_rounds += 1
        else:
            self.previous_signature = signature
            self.repeated_rounds = 1
        return self.repeated_rounds >= 2


# LLM: Progress is derived from typed record mutations and child creation only.
# 函数用途: 判断本轮内部 dispatch 是否产生了结构化状态变化。
def dispatch_made_progress(dispatch_report) -> bool:
    return _dispatch_no_progress_signature(dispatch_report) is None


def _dispatch_no_progress_signature(dispatch_report) -> tuple | None:
    records = list(getattr(dispatch_report, "records", []) or [])
    if not records:
        return ()

    signature = []
    for record in records:
        if _dispatch_record_made_progress(record):
            return None
        record_signature = _dispatch_record_signature(record)
        if record_signature is None:
            return None
        signature.append(record_signature)
    return tuple(signature)


def _dispatch_record_signature(record) -> tuple | None:
    values = (
        _safe_str(record, "step"),
        _safe_str(record, "action"),
        _safe_str(record, "run_id"),
        _safe_str(record, "before_status"),
        _safe_str(record, "after_status"),
        _safe_str(record, "before_verification_status"),
        _safe_str(record, "after_verification_status"),
    )
    if any(value is None for value in values[:2]):
        return None
    return values


def _dispatch_record_made_progress(record) -> bool:
    if _safe_int(record, "runner_created_child_count") > 0:
        return True
    if _safe_list(record, "runner_created_child_ids"):
        return True

    before_status = _safe_str(record, "before_status")
    after_status = _safe_str(record, "after_status")
    if before_status and after_status and before_status != after_status:
        return True

    before_verification = _safe_str(record, "before_verification_status")
    after_verification = _safe_str(record, "after_verification_status")
    if before_verification and after_verification and before_verification != after_verification:
        return True

    return _record_action_is_mutating(record)


def _record_action_is_mutating(record) -> bool:
    step = _safe_str(record, "step") or ""
    action = _safe_str(record, "action") or ""
    applied = _safe_bool(record, "applied")
    if not applied:
        return False
    if step == "action_apply" and action in _RECORD_ONLY_ACTIONS:
        return False
    if (step, action) in _AUDIT_ONLY_ACTIONS:
        return False
    return True


_RECORD_ONLY_ACTIONS = {
    "classify_blocker",
    "inspect_failure",
    "recover_child_after_parent_timeout",
    "stop_no_progress_and_escalate",
    "route_capability_request",
    "triage_capability_gap",
}

_AUDIT_ONLY_ACTIONS = {
    ("due_check", "scan"),
    ("leadership_recovery_plan", "inspect_refs"),
}


def _safe_str(record, field_name: str) -> str | None:
    value = getattr(record, field_name, None)
    if isinstance(value, str):
        return value
    return None


def _safe_bool(record, field_name: str) -> bool:
    value = getattr(record, field_name, False)
    return value if isinstance(value, bool) else False


def _safe_int(record, field_name: str) -> int:
    value = getattr(record, field_name, 0)
    return value if isinstance(value, int) else 0


def _safe_list(record, field_name: str) -> list:
    value = getattr(record, field_name, [])
    return value if isinstance(value, list) else []


__all__ = ["DispatchNoProgressTracker", "dispatch_made_progress"]
