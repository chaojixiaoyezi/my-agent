from __future__ import annotations

from ....contracts.error_taxonomy import error_contract
from ....contracts.state_machine import run_state_snapshot_from_task
from ....runtime_errors import runtime_error_report
from ....subagents.models import (
    SUBAGENT_DISPATCH_READY_STATUSES,
    SUBAGENT_FAILURE_STATUSES,
    TaskStatus,
    task_status_in,
)
from ..run_scope import remembered_orchestration_run_ids


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
        "starting_run_ids": _clean_ids(buckets["starting"]),
        "running_run_ids": _clean_ids(buckets["running"]),
        "blocked_run_ids": _clean_ids(buckets["blocked"]),
        "completed_run_ids": _clean_ids(buckets["completed"]),
        "unfinished_run_ids": _clean_ids(buckets["unfinished"]),
        "missing_run_ids": _clean_ids(missing_run_ids),
        "task_load_errors": load_errors,
        "recovery_recommendations": buckets["recovery_recommendations"],
    }


def _empty_state_buckets() -> dict[str, object]:
    return {
        "by_status": {},
        "dispatchable": [],
        "starting": [],
        "running": [],
        "blocked": [],
        "completed": [],
        "unfinished": [],
        "recovery_recommendations": [],
    }


def _append_task_state(buckets: dict[str, object], task: object) -> None:
    run_id = _run_id(task)
    snapshot = run_state_snapshot_from_task(task)
    status = str(snapshot["status"])
    background_starting = _background_start_in_progress(task, status)
    by_status = buckets["by_status"]
    by_status[status] = by_status.get(status, 0) + 1
    _append_if(buckets["dispatchable"], run_id, bool(snapshot["can_dispatch"]) and not background_starting)
    # 后台 dispatcher 已接收不等于 runner 已进入 RUNNING；两种事实分开，避免主代理提前
    # 向用户声称“N 个子代理正在运行”。
    _append_if(buckets["starting"], run_id, background_starting)
    _append_if(buckets["running"], run_id, task_status_in(status, {TaskStatus.RUNNING.value}))
    _append_if(buckets["blocked"], run_id, task_status_in(status, SUBAGENT_FAILURE_STATUSES))
    completed = bool(snapshot["can_closeout"])
    _append_if(buckets["completed"], run_id, completed)
    _append_if(buckets["unfinished"], run_id, not completed)
    if task_status_in(status, SUBAGENT_FAILURE_STATUSES):
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


# LLM: State receipts describe host-owned lifecycle actions without suggesting
# model polling. Direct-parent wake events carry the next actionable facts.
# 函数用途: 为当前子代理状态标记宿主下一步，不生成查树工具建议。
def _attach_state_next_action(state: dict[str, object]) -> None:
    blocked = list(state.get("blocked_run_ids") or [])
    dispatchable = list(state.get("dispatchable_run_ids") or [])
    running = list(state.get("running_run_ids") or [])
    starting = list(state.get("starting_run_ids") or [])
    unfinished = list(state.get("unfinished_run_ids") or [])
    missing = list(state.get("missing_run_ids") or [])
    load_errors = list(state.get("task_load_errors") or [])
    if blocked:
        state["next_action"] = "handle_direct_child_blocker_from_lifecycle_event"
        return
    if dispatchable:
        state["next_action"] = "wait_for_automatic_runner_start"
        return
    if running:
        state["next_action"] = "wait_for_subagent_completion_event"
        return
    if starting:
        state["next_action"] = "wait_for_subagent_runner_start"
        return
    if load_errors:
        state["next_action"] = "host_rebuild_state_index_or_report_load_error"
        return
    if missing:
        state["next_action"] = "host_reconcile_missing_run_ids"
        return
    if unfinished:
        state["next_action"] = "await_unfinished_run_lifecycle_event"
        return
    state["next_action"] = "summarize_or_report_completed_runs"


def _clean_ids(values: list[object]) -> list[str]:
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _run_id(task: object) -> str:
    return str(getattr(task, "id", "") or "").strip()


def _background_start_in_progress(task: object, status: str) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict) or not task_status_in(status, SUBAGENT_DISPATCH_READY_STATUSES):
        return False
    background = attrs.get("background_start")
    if not isinstance(background, dict):
        return False
    return str(background.get("status") or "").strip() in {"launching", "running"}
