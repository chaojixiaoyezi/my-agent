
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...subagents import SubAgentRunnerResult, SubAgentTask
from ...subagents.services.dispatch.params import DispatchRecordParams
from .child_summary import runner_child_summary_fields

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


__all__ = ["RunnerDispatchRecordParams", "runner_dispatch_record"]
