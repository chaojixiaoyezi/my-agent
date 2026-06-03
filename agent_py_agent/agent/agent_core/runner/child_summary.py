
from __future__ import annotations

from typing import Any

from ...runtime_errors import runtime_error_report
from ...subagent import SubAgentRunnerResult, SubAgentTask

_RUNNER_CHILD_FINAL_STATUSES = {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}


def runner_child_summary_fields(agent: Any, after: SubAgentTask, result: SubAgentRunnerResult) -> dict[str, object]:
    child_ids = [str(item) for item in (after.child_ids or []) if str(item).strip()]
    child_states = _runner_child_states(agent, child_ids)
    return {
        "runner_summary": result.structured_summary,
        "runner_created_child_count": len(child_ids),
        "runner_created_child_ids": child_ids,
        "runner_created_roles": [item["role"] for item in child_states if item["role"]],
        "runner_child_status_counts": _runner_child_status_counts(child_states),
        "runner_unfinished_child_ids": _runner_unfinished_child_ids(child_states),
        "runner_child_load_errors": _runner_child_load_errors(child_states),
        "runner_partial_success": bool(child_ids and not result.ok),
    }


def _runner_child_states(agent: Any, child_ids: list[str]) -> list[dict[str, object]]:
    states: list[dict[str, object]] = []
    for child_id in child_ids:
        try:
            child = agent.subagents.load(child_id)
        except Exception as exc:
            states.append({
                "id": child_id,
                "role": "",
                "status": "UNKNOWN",
                "load_error": runtime_error_report(exc, context="subagents.load"),
            })
            continue
        states.append({
            "id": child_id,
            "role": str(getattr(child, "role", "") or "").strip(),
            "status": str(getattr(child, "status", "") or "UNKNOWN").strip().upper(),
        })
    return states


def _runner_child_status_counts(child_states: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in child_states:
        status = str(item["status"] or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _runner_unfinished_child_ids(child_states: list[dict[str, object]]) -> list[str]:
    return [
        str(item["id"]) for item in child_states
        if item["id"] and str(item["status"]) not in _RUNNER_CHILD_FINAL_STATUSES
    ]


def _runner_child_load_errors(child_states: list[dict[str, object]]) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    for item in child_states:
        error = item.get("load_error")
        if not isinstance(error, dict):
            continue
        errors.append({"run_id": str(item.get("id") or ""), **error})
    return errors
