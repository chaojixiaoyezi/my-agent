
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagents.models import SubAgentCapabilityRouteOptions, SubAgentDueCheckOptions
from ..subagents.services.dispatch_params import DispatchRecordParams, DispatchWatchRecordParams
from .dispatch_record_params import (
    AcceptanceRecordParams,
    ActionApplyRecordParams,
    CapabilityRouteRecordParams,
    PatchReviewRecordParams,
)
from .dispatch_workflow_records import build_workflow_records

if TYPE_CHECKING:
    from ..core import SimpleAgent


# ---------------------------------------------------------------------------
# Watch mode helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MakeDispatchWatchRecordParams:

    cycle: int
    dry_run: bool
    ok: bool
    message: str
    dispatch_record_count: int
    dispatch_summary: dict[str, int] | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    evidence_paths: list[str] | None = None


# ---------------------------------------------------------------------------
# Step record builders
# ---------------------------------------------------------------------------


def make_due_check_record(agent, cfg, apply):
    options = SubAgentDueCheckOptions(config=cfg, write_report=apply)
    due_report = (
        agent.subagents.write_due_check(params=options)
        if apply
        else agent.subagents.due_check(params=options)
    )
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
        step="due_check",
        action="scan",
        dry_run=not apply,
        applied=False,
        ok=True,
        message=f"发现 {due_report.summary.get('total', 0)} 个 due-check issue。",
        evidence_paths=[str(agent.subagents.workspace / "subagent_due_check.json")],
        ),
    )


def make_action_apply_records(params: ActionApplyRecordParams):
    agent = params.agent
    records = []
    action_report = (
        agent.subagents.write_action_apply_report(
            params.cfg,
            apply=params.apply,
            take_over_by=params.take_over_by,
            locked_files=params.locked_files or [],
            limit=params.limit,
        )
        if params.apply
        else agent.subagents.apply_actions(
            params.cfg,
            apply=False,
            take_over_by=params.take_over_by,
            locked_files=params.locked_files or [],
            limit=params.limit,
        )
    )
    for item in action_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
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
        )
    return records


def make_capability_route_records(params: CapabilityRouteRecordParams):
    agent = params.agent
    records = []
    options = SubAgentCapabilityRouteOptions(
        apply=params.apply,
        limit=params.limit,
    )
    route_report = (
        agent.subagents.write_capability_route_report(
            params.router,
            params.cfg,
            params=options,
        )
        if params.apply
        else agent.subagents.route_capability_requests(
            params.router,
            params.cfg,
            params=options,
        )
    )
    for item in route_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
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


def make_patch_review_records(params: PatchReviewRecordParams):
    agent = params.agent
    if not params.patch_run_ids:
        return []
    records = []
    patch_report = (
        agent.subagents.write_patch_review_report(
            params.patch_run_ids,
            apply=True,
            reviewer=params.reviewer,
            note=params.note,
            limit=params.limit,
        )
        if params.apply
        else agent.subagents.review_patches(
            params.patch_run_ids,
            apply=False,
            reviewer=params.reviewer,
            note=params.note,
            limit=params.limit,
        )
    )
    for item in patch_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
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


def make_acceptance_records(params: AcceptanceRecordParams):
    agent = params.agent
    records = []
    acceptance_report = (
        agent.subagents.write_acceptance_review_report(
            apply=True,
            reviewer=params.reviewer,
            note=params.note,
            limit=params.limit,
        )
        if params.apply
        else agent.subagents.review_acceptances(
            apply=False,
            reviewer=params.reviewer,
            note=params.note,
            limit=params.limit,
        )
    )
    for item in acceptance_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
                params=DispatchRecordParams(
                step="acceptance",
                action=item.decision.lower(),
                run_id=item.run_id,
                dry_run=item.dry_run,
                applied=item.applied,
                ok=item.ok,
                message=item.message,
                before_status=item.before_status,
                after_status=item.after_status,
                before_verification_status=item.before_verification_status,
                after_verification_status=item.after_verification_status,
                evidence_paths=item.evidence_paths,
                ),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Pending work state management
# ---------------------------------------------------------------------------


def update_pending_work_state(agent) -> bool:
    from .runner_dispatch import _dispatch_runner_candidates, _runner_max_attempts

    runner_max_attempts = _runner_max_attempts(agent.config.runner_failure_policy)
    candidates = _dispatch_runner_candidates(
        agent.subagents.list_runs(),
        max_runners=999,
        runner_max_attempts=runner_max_attempts,
    )
    return len(candidates) > 0


# ---------------------------------------------------------------------------
# Watch mode helpers
# ---------------------------------------------------------------------------


def make_dispatch_watch_record(
    agent,
    params: MakeDispatchWatchRecordParams,
) -> DispatchWatchRecord:
    return agent.subagents.make_dispatch_watch_record(
        params=DispatchWatchRecordParams(
            cycle=params.cycle,
            dry_run=params.dry_run,
            ok=params.ok,
            message=params.message,
            dispatch_record_count=params.dispatch_record_count,
            dispatch_summary=params.dispatch_summary,
            started_at=params.started_at,
            ended_at=params.ended_at,
            evidence_paths=params.evidence_paths,
        ),
    )
