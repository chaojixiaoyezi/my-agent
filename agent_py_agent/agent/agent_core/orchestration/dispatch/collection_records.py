
from __future__ import annotations

from ....subagents.services.dispatch.params import DispatchRecordParams
from .params import DispatchExecutionPlan
from .runner_selection import scoped_runner_tasks
from .service import (
    build_workflow_records,
    make_action_apply_records,
    make_capability_route_records,
    make_due_check_record,
    make_leadership_recovery_plan_record,
)


def collect_dispatch_records(agent, ctx):
    records = []
    records.extend(_planner_and_workflow_records(agent, ctx))
    records.extend(_due_and_leadership_records(agent, ctx))
    records.extend(_action_apply_records(agent, ctx))
    records.extend(_capability_route_records(agent, ctx))
    return records


def _planner_and_workflow_records(agent, ctx) -> list:
    records = []
    if ctx.planner:
        records.append(parent_planner_dispatch_record(agent, ctx))
    if ctx.normalized_workflow_mode in {"plan", "auto"}:
        records.extend(
            build_workflow_records(
                agent,
                ctx,
                scoped_runner_tasks(agent.subagents.list_runs(), ctx),
                override_task_off=True,
            )
        )
    return records


def parent_planner_dispatch_record(agent, ctx):
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
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="parent_planner",
            action=planner_record.decision.lower(),
            dry_run=ctx.preview_only,
            applied=False,
            ok=planner_record.ok,
            message=planner_record.message,
            evidence_paths=planner_record.evidence_paths,
            runner_instruction=planner_record.runner_instruction,
            suggested_max_runners=planner_record.suggested_max_runners,
        ),
    )


def _due_and_leadership_records(agent, ctx) -> list:
    records = [make_due_check_record(agent, ctx)]
    leadership_record = None if _has_explicit_dispatch_scope(ctx) else make_leadership_recovery_plan_record(agent, ctx.cfg)
    if leadership_record is not None:
        records.append(leadership_record)
    return records


def _has_explicit_dispatch_scope(ctx) -> bool:
    return bool(ctx.parent_run_id or ctx.root_id or ctx.include_run_ids or ctx.exclude_run_ids)


def _action_apply_records(agent, ctx) -> list:
    return make_action_apply_records(agent, ctx)


def _capability_route_records(agent, ctx) -> list:
    return make_capability_route_records(agent, ctx)
