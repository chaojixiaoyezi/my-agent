from __future__ import annotations

"""LLM: helper records for the dispatch mixin facade."""

from dataclasses import dataclass

from ..subagents.services.dispatch_params import DispatchRecordParams
from .dispatch_params import DispatchContext, DispatchParams


@dataclass(frozen=True)
class RunnerJobExecutionParams:
    ctx: DispatchContext
    execute_runners: bool
    max_cards: int
    probe: bool
    existing_records: list


@dataclass(frozen=True)
class DispatchRunnerStageRequest:
    # LLM: dispatch runner stage inputs are bundled for code-size and future scheduling fields.
    agent: object
    ctx: DispatchContext
    params: DispatchParams
    records: list


def parent_planner_dispatch_record(agent, ctx: DispatchContext):
    from .subagent_mixin import RunParentPlannerParams

    planner_record = agent.run_parent_planner(
        RunParentPlannerParams(
            router=ctx.router,
            capability_config=ctx.cfg,
            apply=ctx.apply,
            execute_runners=False,
            max_runners=ctx.max_runners,
            limit=ctx.limit,
            reviewer=ctx.reviewer,
            note=ctx.note,
            runner_instruction=ctx.runner_instruction,
        )
    )
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="parent_planner",
            action=planner_record.decision.lower(),
            dry_run=not ctx.apply,
            applied=False,
            ok=planner_record.ok,
            message=planner_record.message,
            evidence_paths=planner_record.evidence_paths,
        ),
    )


def run_dispatch_runner_stage(
    agent=None,
    *,
    request: DispatchRunnerStageRequest | None = None,
    ctx: DispatchContext | None = None,
    params: DispatchParams | None = None,
    records: list | None = None,
) -> list:
    # LLM: runner execution params stay bundled away from the dispatch facade.
    request = request or DispatchRunnerStageRequest(agent, ctx, params, records or [])
    return request.agent._execute_runner_jobs(
        RunnerJobExecutionParams(
            ctx=request.ctx,
            execute_runners=request.params.execute_runners,
            max_cards=request.params.max_cards,
            probe=request.params.probe,
            existing_records=request.records,
        )
    )
