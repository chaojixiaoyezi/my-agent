
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...runtime_errors import runtime_error_report
from ...subagents import SubAgentRunnerResult, SubAgentTask
from ...subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from ...subagents.services.dispatch.params import DispatchRecordParams

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class RunnerDispatchRecordParams:
    agent: SimpleAgent
    run_id: str
    before: SubAgentTask
    after: SubAgentTask
    result: SubAgentRunnerResult
    retry_reason: str
    start_runner: bool


def runner_dispatch_record(params: RunnerDispatchRecordParams):
    return params.agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner",
            action=_runner_action(params),
            run_id=params.run_id,
            dry_run=params.result.dry_run,
            applied=not params.result.dry_run,
            ok=params.result.ok,
            message=params.result.message,
            before_status=params.before.status,
            after_status=params.after.status,
            before_verification_status=params.before.verification_status,
            after_verification_status=params.after.verification_status,
            evidence_paths=[
                params.result.execution_context_json,
                params.result.result_json,
                params.result.output_json,
            ],
            **runner_child_summary_fields(params.agent, params.after, params.result),
        ),
    )


def _runner_action(params: RunnerDispatchRecordParams) -> str:
    if params.retry_reason and params.start_runner:
        return "retry_runner"
    if params.start_runner:
        return "execute_runner"
    return "runner_dry_run"


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
        if item["id"] and not task_status_in(item["status"], SUBAGENT_ENDED_STATUSES)
    ]


def _runner_child_load_errors(child_states: list[dict[str, object]]) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    for item in child_states:
        error = item.get("load_error")
        if not isinstance(error, dict):
            continue
        errors.append({"run_id": str(item.get("id") or ""), **error})
    return errors


__all__ = ["RunnerDispatchRecordParams", "runner_dispatch_record"]
