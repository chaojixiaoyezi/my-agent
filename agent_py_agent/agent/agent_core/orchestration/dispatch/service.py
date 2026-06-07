

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ....subagents.models import (
    SubAgentCapabilityRouteOptions,
    SubAgentDueCheckOptions,
    SubAgentLeadershipRecoveryPlanOptions,
)
from ....subagents.services.actions import ActionApplyOptions
from ....subagents.services.dispatch.params import DispatchRecordParams, DispatchWatchRecordParams
from .workflow_records import build_workflow_records

if TYPE_CHECKING:
    from ..core import SimpleAgent


# ---------------------------------------------------------------------------
# Step record builders
# ---------------------------------------------------------------------------


def make_due_check_record(agent: Any, ctx: Any):
    options = SubAgentDueCheckOptions(
        config=ctx.cfg,
        write_report=ctx.mutate_state,
        root_id=ctx.root_id,
        include_run_ids=list(ctx.include_run_ids or []),
        exclude_run_ids=list(ctx.exclude_run_ids or []),
    )
    due_report = (
        agent.subagents.board.write_due_check(params=options)
        if ctx.mutate_state
        else agent.subagents.board.due_check(params=options)
    )
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="due_check",
            action="scan",
            dry_run=not ctx.mutate_state,
            applied=False,
            ok=True,
            message=f"发现 {due_report.summary.get('total', 0)} 个 due-check issue。",
            evidence_paths=[str(agent.subagents.workspace / "subagent_due_check.json")],
        ),
    )


def make_leadership_recovery_plan_record(agent, cfg):
    options = SubAgentLeadershipRecoveryPlanOptions(config=cfg, write_report=True)
    plan = agent.subagents.hierarchy.write_leadership_recovery_plan(params=options)
    affected = plan.summary.get("stale_coordinators", 0) + plan.summary.get("failed_parent_nodes", 0)
    if affected <= 0:
        return None
    plan_ref = str(agent.subagents.workspace / "subagent_leadership_recovery_plan.json")
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="leadership_recovery_plan",
            action="inspect_refs",
            dry_run=True,
            applied=False,
            ok=True,
            message=(
                f"发现 {affected} 个需要领导权恢复计划的父节点；"
                f"assigned_children={plan.summary.get('assigned_children', 0)} "
                f"unassigned_children={plan.summary.get('unassigned_children', 0)}。"
            ),
            evidence_paths=[
                plan_ref,
                str(agent.subagents.workspace / "SUBAGENT_LEADERSHIP_RECOVERY_PLAN.md"),
            ],
        ),
    )


def make_action_apply_records(agent: Any, ctx: Any):
    records = []
    action_options = _action_apply_options(ctx)
    action_report = _action_apply_report(agent, ctx, action_options)
    for item in action_report.records:
        records.append(_action_apply_dispatch_record(agent, item))
    return records


def _action_apply_options(ctx: Any) -> ActionApplyOptions:
    return ActionApplyOptions(
        apply=ctx.mutate_state,
        take_over_by=ctx.take_over_by or "",
        locked_files=ctx.locked_files or [],
        limit=ctx.limit,
        root_id=ctx.root_id,
        include_run_ids=list(ctx.include_run_ids or []),
        exclude_run_ids=list(ctx.exclude_run_ids or []),
    )


def _action_apply_report(agent: Any, ctx: Any, options: ActionApplyOptions):
    if ctx.mutate_state:
        return agent.subagents.actions.write_action_apply_report(ctx.cfg, options=options)
    return agent.subagents.actions.apply_actions(ctx.cfg, options=options)


def _action_apply_dispatch_record(agent: Any, item):
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="action_apply",
            action=item.action,
            run_id=item.run_id,
            dry_run=item.dry_run,
            applied=item.applied,
            ok=item.ok,
            message=item.message,
            before_status=item.before_status,
            after_status=item.after_status,
            evidence_paths=item.evidence_paths,
        ),
    )


def make_capability_route_records(agent: Any, ctx: Any, *, mutate_state: bool | None = None):
    records = []
    should_apply = ctx.mutate_state if mutate_state is None else bool(mutate_state)
    options = SubAgentCapabilityRouteOptions(
        apply=should_apply,
        limit=ctx.limit,
    )
    route_report = (
        agent.subagents.capability.write_capability_route_report(
            ctx.router,
            ctx.cfg,
            params=options,
        )
        if should_apply
        else agent.subagents.capability.route_capability_requests(
            ctx.router,
            ctx.cfg,
            params=options,
        )
    )
    for item in route_report.records:
        records.append(
            agent.subagents.dispatch.make_dispatch_record(
                params=DispatchRecordParams(
                    step="capability_route",
                    action=item.status.lower(),
                    run_id=item.run_id,
                    dry_run=item.dry_run,
                    applied=not item.dry_run,
                    ok=item.status in {"WOULD_GRANT", "GRANTED"},
                    message=item.message,
                    evidence_paths=[
                        str(agent.subagents.workspace / "subagent_capability_route_report.json")
                    ],
                ),
            )
        )
    return records


def make_patch_review_records(agent: Any, patch_run_ids: list[str], params: Any):
    if not patch_run_ids:
        return []
    records = []
    patch_report = (
        agent.subagents.patch.write_patch_review_report(
            patch_run_ids,
            apply=True,
            reviewer=params.reviewer,
            note=params.note,
            limit=params.limit,
        )
        if params.mutate_state
        else agent.subagents.patch.review_patches(
            patch_run_ids,
            apply=False,
            reviewer=params.reviewer,
            note=params.note,
            limit=params.limit,
        )
    )
    for item in patch_report.records:
        records.append(
            agent.subagents.dispatch.make_dispatch_record(
                params=DispatchRecordParams(
                    step="patch_review",
                    action=item.decision.lower(),
                    run_id=item.run_id,
                    dry_run=item.dry_run,
                    applied=item.applied,
                    ok=item.ok,
                    message=item.message,
                    evidence_paths=item.evidence_paths,
                ),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Pending work state management
# ---------------------------------------------------------------------------


def update_pending_work_state(agent) -> bool:
    from ...runner.dispatch import (
        RunnerCandidatePolicy,
        _dispatch_runner_candidates,
        _runner_max_attempts,
        _same_run_redispatch_limit,
    )

    runtime_policy = getattr(agent, "runtime_guard_policy", None)
    runner_max_attempts = _runner_max_attempts(
        agent.config.runner_failure_policy,
        runtime_policy=runtime_policy,
    )
    same_run_limit = _same_run_redispatch_limit(
        getattr(agent.config, "same_run_redispatch_limit", None),
        runtime_policy=runtime_policy,
    )
    candidates = _dispatch_runner_candidates(
        agent.subagents.list_runs(),
        max_runners=999,
        policy=RunnerCandidatePolicy(
            runner_max_attempts=runner_max_attempts,
            same_run_redispatch_limit=same_run_limit,
        ),
    )
    return len(candidates) > 0


# ---------------------------------------------------------------------------
# Watch mode helpers
# ---------------------------------------------------------------------------


def make_dispatch_watch_record(
    agent,
    params: DispatchWatchRecordParams,
) -> DispatchWatchRecord:
    return agent.subagents.dispatch.make_dispatch_watch_record(params=params)
