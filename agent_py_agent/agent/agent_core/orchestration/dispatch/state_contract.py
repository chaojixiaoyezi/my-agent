from __future__ import annotations

from ....contracts.error_taxonomy import error_contract
from ....contracts.state_machine import run_state_snapshot_from_task
from ....runtime_errors import runtime_error_report
from ..run_scope import remembered_orchestration_run_ids

_RUNNING_STATUSES = {"RUNNING"}
_BLOCKED_STATUSES = {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}


def dispatch_state_contract_payload(agent: object) -> dict[str, object]:
    tasks, missing, load_errors = _remembered_tasks(agent)
    if not tasks and not missing and not load_errors:
        return {}
    state = _state_payload(tasks, missing, load_errors)
    _attach_state_next_action(state)
    return {"current_turn_run_state": state}


def _remembered_tasks(agent: object) -> tuple[list[object], list[str], list[dict[str, object]]]:
    load = getattr(getattr(agent, "subagents", None), "load", None)
    if not callable(load):
        return [], [], []
    tasks: list[object] = []
    missing: list[str] = []
    load_errors: list[dict[str, object]] = []
    for run_id in sorted(remembered_orchestration_run_ids(agent)):
        try:
            task = load(run_id)
        except Exception as exc:
            missing.append(run_id)
            load_errors.append({"run_id": run_id, **runtime_error_report(exc, context="subagents.load")})
            continue
        tasks.append(task)
    return tasks, missing, load_errors


def _state_payload(
    tasks: list[object],
    missing_run_ids: list[str],
    load_errors: list[dict[str, object]],
) -> dict[str, object]:
    buckets = _empty_state_buckets()
    for task in tasks:
        _append_task_state(buckets, task)
    return {
        "state_machine_contract": "state_machine.v1",
        "total": len(tasks),
        "by_status": buckets["by_status"],
        "dispatchable_run_ids": _clean_ids(buckets["dispatchable"]),
        "running_run_ids": _clean_ids(buckets["running"]),
        "blocked_run_ids": _clean_ids(buckets["blocked"]),
        "verified_run_ids": _clean_ids(buckets["verified"]),
        "unfinished_run_ids": _clean_ids(buckets["unfinished"]),
        "missing_run_ids": _clean_ids(missing_run_ids),
        "task_load_errors": load_errors,
        "recovery_recommendations": buckets["recovery_recommendations"],
    }


def _empty_state_buckets() -> dict[str, object]:
    return {
        "by_status": {},
        "dispatchable": [],
        "running": [],
        "blocked": [],
        "verified": [],
        "unfinished": [],
        "recovery_recommendations": [],
    }


def _append_task_state(buckets: dict[str, object], task: object) -> None:
    run_id = _run_id(task)
    snapshot = run_state_snapshot_from_task(task)
    status = str(snapshot["status"])
    background_running = _background_start_running(task, status)
    by_status = buckets["by_status"]
    by_status[status] = by_status.get(status, 0) + 1
    _append_if(buckets["dispatchable"], run_id, bool(snapshot["can_dispatch"]) and not background_running)
    _append_if(buckets["running"], run_id, status in _RUNNING_STATUSES or background_running)
    _append_if(buckets["blocked"], run_id, status in _BLOCKED_STATUSES)
    verified = bool(snapshot["can_closeout"])
    _append_if(buckets["verified"], run_id, verified)
    _append_if(buckets["unfinished"], run_id, not verified)
    if status in _BLOCKED_STATUSES:
        _append_recovery_recommendation(buckets, snapshot)


def _append_if(values: object, run_id: str, condition: bool) -> None:
    if condition and run_id and isinstance(values, list):
        values.append(run_id)


def _append_recovery_recommendation(buckets: dict[str, object], snapshot: dict[str, object]) -> None:
    values = buckets["recovery_recommendations"]
    run_id = str(snapshot.get("run_id") or "").strip()
    if not run_id or not isinstance(values, list):
        return
    decision = dict(snapshot.get("recovery_decision") or {})
    contract = error_contract(str(snapshot.get("failure_type") or "UNKNOWN_ERROR"))
    values.append(
        {
            "run_id": run_id,
            "status": str(snapshot.get("status") or ""),
            "failure_type": contract.code,
            "recommended_action": str(decision.get("action") or ""),
            "allow_new_run": bool(decision.get("allow_new_run")),
            "reason": str(decision.get("reason") or ""),
            "recovery_hint": contract.recovery_hint,
        }
    )


def _attach_state_next_action(state: dict[str, object]) -> None:
    blocked = list(state.get("blocked_run_ids") or [])
    dispatchable = list(state.get("dispatchable_run_ids") or [])
    running = list(state.get("running_run_ids") or [])
    missing = list(state.get("missing_run_ids") or [])
    load_errors = list(state.get("task_load_errors") or [])
    if blocked:
        state["next_action"] = "inspect_or_rescue_blocked_run_ids"
        state["suggested_tool_call"] = _dispatch_tool_call(blocked, start_runners=False)
        return
    if dispatchable:
        state["next_action"] = "continue_dispatch_unfinished_run_ids"
        state["suggested_tool_call"] = _dispatch_tool_call(dispatchable, start_runners=True)
        return
    if running:
        state["next_action"] = "wait_for_subagent_completion_event"
        state["suggested_tool_call"] = {"tool": "wait", "seconds": 120, "reason": "等待运行中的子代理完成或产出新事件"}
        return
    if load_errors:
        state["next_action"] = "refresh_agent_tree_or_rebuild_state_index"
        state["suggested_tool_call"] = {"tool": "inspect_agent_tree"}
        return
    if missing:
        state["next_action"] = "refresh_agent_tree_for_missing_run_ids"
        state["suggested_tool_call"] = {"tool": "inspect_agent_tree"}
        return
    state["next_action"] = "summarize_or_report_verified_runs"


def _dispatch_tool_call(run_ids: list[str], *, start_runners: bool) -> dict[str, object]:
    return {
        "tool": "dispatch_subagents",
        "dry_run": not start_runners,
        "run_ids": _clean_ids(run_ids),
        "workflow_mode": "off",
    }


def _clean_ids(values: list[object]) -> list[str]:
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _run_id(task: object) -> str:
    return str(getattr(task, "id", "") or "").strip()


def _background_start_running(task: object, status: str) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict) or status not in {"PLANNING", "PENDING"}:
        return False
    background = attrs.get("background_start")
    if not isinstance(background, dict):
        return False
    return str(background.get("status") or "").strip().lower() in {"launching", "running"}
