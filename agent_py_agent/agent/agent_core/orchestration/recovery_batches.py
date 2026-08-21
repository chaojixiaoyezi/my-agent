from __future__ import annotations

from ...subagents.services.recovery.modes import (
    RecoveryMode,
    is_rerun_mode,
    is_takeover_mode,
    recovery_mode_from_protocol_value,
)


def recovery_batches_from_strategies(strategies: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[RecoveryMode, list[dict[str, object]]] = {}
    for item in strategies:
        mode = recovery_mode_from_protocol_value(item.get("recovery_mode"))
        groups.setdefault(mode, []).append(item)
    return [_batch_payload(mode, items) for mode, items in groups.items()]


def recovery_counts_by_field(strategies: list[dict[str, object]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in strategies:
        value = str(item.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _batch_payload(mode: RecoveryMode, items: list[dict[str, object]]) -> dict[str, object]:
    run_ids = _run_ids(items)
    action = _recommended_action(items)
    payload: dict[str, object] = {
        "recovery_mode": mode.value,
        "recommended_action": action,
        "run_ids": run_ids,
        "count": len(run_ids),
        "execution_mode": _execution_mode(mode),
        "system_recovery": _system_recovery(mode, run_ids, items),
    }
    if mode is RecoveryMode.LEADERSHIP_RECOVERY:
        payload["requires_leader_selection"] = True
        payload["child_run_ids_by_leader"] = _child_run_ids_by_leader(items)
    if mode is RecoveryMode.NO_PROGRESS_LIMIT_REACHED:
        payload["must_not_auto_retry"] = True
    return {key: value for key, value in payload.items() if value not in ({}, [], "")}


def _recommended_action(items: list[dict[str, object]]) -> str:
    for item in items:
        action = str(item.get("recommended_action") or "").strip()
        if action:
            return action
    return "manual_review"


def _execution_mode(mode: RecoveryMode) -> str:
    if is_rerun_mode(mode):
        return "rerun_original"
    if is_takeover_mode(mode):
        return "takeover_apply"
    if mode is RecoveryMode.LEADERSHIP_RECOVERY:
        return "leader_recovery"
    if mode is RecoveryMode.NO_PROGRESS_LIMIT_REACHED:
        return "stop_and_report"
    return "manual_review"


def _system_recovery(
    mode: RecoveryMode,
    run_ids: list[str],
    items: list[dict[str, object]],
) -> dict[str, object]:
    if is_rerun_mode(mode):
        return _rerun_recovery(run_ids, items)
    if is_takeover_mode(mode):
        return {
            "action": "await_parent_takeover_decision",
            "run_ids": list(run_ids),
        }
    return {}


def _rerun_recovery(
    run_ids: list[str],
    items: list[dict[str, object]],
) -> dict[str, object]:
    recovery: dict[str, object] = {
        "action": "auto_retry_original_run",
        "run_ids": list(run_ids),
    }
    mode = recovery_mode_from_protocol_value(items[0].get("recovery_mode")) if len(items) == 1 else None
    if mode is not None:
        recovery["recovery_mode"] = mode.value
    if len(run_ids) == 1 and len(items) == 1:
        instruction = str(items[0].get("runner_instruction") or "").strip()
        if instruction:
            recovery["runner_instruction"] = instruction
    return recovery


def _run_ids(items: list[dict[str, object]]) -> list[str]:
    result: list[str] = []
    for item in items:
        run_id = str(item.get("run_id") or "").strip()
        if run_id and run_id not in result:
            result.append(run_id)
    return result


def _child_run_ids_by_leader(items: list[dict[str, object]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for item in items:
        run_id = str(item.get("run_id") or "").strip()
        if not run_id:
            continue
        children = [str(child) for child in item.get("child_run_ids") or [] if str(child).strip()]
        mapping[run_id] = children
    return mapping


__all__ = ["recovery_batches_from_strategies", "recovery_counts_by_field"]
