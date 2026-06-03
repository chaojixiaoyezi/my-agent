
from __future__ import annotations

from .mixin_helpers import parent_planner_dispatch_record
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
