from __future__ import annotations

from typing import Any

from ...subagents.services.recovery.modes import (
    LEADERSHIP_RECOVERY,
    NO_PROGRESS_LIMIT_REACHED,
    is_rerun_mode,
    is_takeover_mode,
)


def recovery_batches_from_strategies(strategies: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for item in strategies:
        mode = str(item.get("recovery_mode") or "manual_review_missing_recovery_refs")
        groups.setdefault(mode, []).append(item)
    return [_batch_payload(mode, items) for mode, items in groups.items()]


def recovery_counts_by_field(strategies: list[dict[str, object]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in strategies:
        value = str(item.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _batch_payload(mode: str, items: list[dict[str, object]]) -> dict[str, object]:
    run_ids = _run_ids(items)
    action = _recommended_action(items)
    payload: dict[str, object] = {
        "recovery_mode": mode,
        "recommended_action": action,
        "run_ids": run_ids,
        "count": len(run_ids),
        "execution_mode": _execution_mode(mode),
        "suggested_tool_call": _suggested_tool_call(mode, run_ids, items),
    }
    if mode == LEADERSHIP_RECOVERY:
        payload["requires_leader_selection"] = True
        payload["child_run_ids_by_leader"] = _child_run_ids_by_leader(items)
    if mode == NO_PROGRESS_LIMIT_REACHED:
        payload["must_not_auto_retry"] = True
    return {key: value for key, value in payload.items() if value not in ({}, [], "")}


def _recommended_action(items: list[dict[str, object]]) -> str:
    for item in items:
        action = str(item.get("recommended_action") or "").strip()
        if action:
            return action
    return "manual_review"


def _execution_mode(mode: str) -> str:
    if is_rerun_mode(mode):
        return "rerun_original"
    if is_takeover_mode(mode):
        return "takeover_apply"
    if mode == LEADERSHIP_RECOVERY:
        return "leader_recovery"
    if mode == NO_PROGRESS_LIMIT_REACHED:
        return "stop_and_report"
    return "manual_review"


def _suggested_tool_call(mode: str, run_ids: list[str], items: list[dict[str, object]]) -> dict[str, object]:
    if is_rerun_mode(mode):
        return _rerun_tool_call(run_ids, items)
    if is_takeover_mode(mode):
        return _takeover_tool_call(run_ids)
    return {}


def _rerun_tool_call(run_ids: list[str], items: list[dict[str, object]]) -> dict[str, object]:
    call = _dispatch_tool_call(run_ids, start_runners=True)
    mode = str(items[0].get("recovery_mode") or "") if len(items) == 1 else ""
    if mode:
        call["recovery_mode"] = mode
    if len(run_ids) == 1 and len(items) == 1:
        instruction = str(items[0].get("runner_instruction") or "").strip()
        if instruction:
            call["runner_instruction"] = instruction
    return call


def _takeover_tool_call(run_ids: list[str]) -> dict[str, object]:
    call = _dispatch_tool_call(run_ids, start_runners=False)
    call["max_runners"] = 0
    return call


def _dispatch_tool_call(run_ids: list[str], *, start_runners: bool) -> dict[str, Any]:
    return {
        "tool": "dispatch_subagents",
        "dry_run": not start_runners,
        "run_ids": list(run_ids),
        "workflow_mode": "off",
    }


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
