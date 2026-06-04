
from __future__ import annotations

"""Helper functions for dispatch mixin stages."""

from ....subagents.services.dispatch.params import DispatchRecordParams
from .params import DispatchContext, DispatchExecutionPlan, DispatchParams


def parent_planner_dispatch_record(agent, ctx: DispatchContext):
    from ..._subagent_planner_mixin import RunParentPlannerParams

    planner_record = agent.run_parent_planner(
        RunParentPlannerParams(
            router=ctx.router,
            capability_config=ctx.cfg,
            execution_plan=DispatchExecutionPlan(
                preview_only=ctx.execution_plan.preview_only,
                mutate_state=ctx.execution_plan.mutate_state,
                start_runners=False,
                max_runners=ctx.execution_plan.max_runners,
            ),
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
            dry_run=ctx.preview_only,
            applied=False,
            ok=planner_record.ok,
            message=planner_record.message,
            evidence_paths=planner_record.evidence_paths,
        ),
    )


def run_dispatch_runner_stage(
    agent=None,
    *,
    ctx: DispatchContext | None = None,
    params: DispatchParams | None = None,
    records: list | None = None,
) -> list:
    if agent is None or ctx is None or params is None:
        raise TypeError("run_dispatch_runner_stage requires agent, ctx, and params")
    return agent._execute_runner_jobs(
        ctx=ctx,
        max_cards=params.max_cards,
        probe=params.probe,
        records=records or [],
    )
