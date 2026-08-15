
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DispatchNoProgressTracker:
    previous_signature: tuple | None = None
    repeated_rounds: int = 0

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


def dispatch_made_progress(dispatch_report) -> bool:
    return _dispatch_no_progress_signature(dispatch_report) is None


def dispatch_no_progress_payload(dispatch_report) -> dict[str, object]:
    signature = _dispatch_no_progress_signature(dispatch_report)
    if signature is None or not signature:
        return {}
    records = list(getattr(dispatch_report, "records", []) or [])
    payload = {
        "no_progress_actions_only": True,
        "recommended_next_action": "summarize_blockers_or_change_strategy",
        "reason": (
            "本轮 dispatch 只有 due-check、inspect 或 classify 等记录类动作；"
            "没有创建子代理、状态变化或验收执行。请不要原样重复 dispatch，改为查看 refs、换策略或汇报 blockers。"
        ),
        "record_count": len(records),
        "blocked_run_ids": _record_run_ids(records),
    }
    if tool_call := _dry_run_recovery_tool_call(records):
        payload["recommended_next_action"] = "rerun_dispatch_with_apply_for_recovery"
        payload["reason"] = (
            "本轮只做了 dry-run，已经发现可写回的恢复动作。"
            "如果当前父级确实要接管/重分配，请按 suggested_tool_call 重新调用；"
            "runner 内部会默认用当前父级 run_id 作为 take_over_by。"
        )
        payload["suggested_tool_call"] = tool_call
    return payload


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


def _record_run_ids(records: list[object]) -> list[str]:
    run_ids: list[str] = []
    for record in records:
        run_id = _safe_str(record, "run_id") or ""
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
    return run_ids[:20]


def _dry_run_recovery_tool_call(records: list[object]) -> dict[str, object]:
    if not any(_is_dry_run_recovery_apply(record) for record in records):
        return {}
    return {
        "tool": "dispatch_subagents",
        "dry_run": False,
        "max_runners": 0,
        "limit": max(len(records), 1),
    }


def _is_dry_run_recovery_apply(record: object) -> bool:
    return (
        _safe_str(record, "step") == "action_apply"
        and _safe_str(record, "action") in {"takeover_or_reassign", "recover_coordinator_leadership"}
        and _safe_bool(record, "dry_run")
        and not _safe_bool(record, "applied")
    )


__all__ = ["DispatchNoProgressTracker", "dispatch_made_progress", "dispatch_no_progress_payload"]
