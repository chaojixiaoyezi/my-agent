
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagents.models import SubAgentCapabilityRouteOptions, SubAgentDueCheckOptions
from ..subagents.services.dispatch_params import DispatchRecordParams, DispatchWatchRecordParams
from ..subagents.services.workflow import _try_workflow_plan, _workflow_extra_write_roots
from .dispatch_record_params import (
    AcceptanceRecordParams,
    ActionApplyRecordParams,
    CapabilityRouteRecordParams,
    DryRunWorkflowRecordParams,
    PatchReviewRecordParams,
    WorkflowRecordParams,
)

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


def build_workflow_records(params: WorkflowRecordParams):
    workflow_candidates = [
        task
        for task in params.tasks
        if not task.parent_id
        and not task.workflow_parent_run_id
        and task.status not in {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
    ]
    if params.limit > 0:
        workflow_candidates = workflow_candidates[: params.limit]
    records = []
    for task in workflow_candidates:
        task_records = _build_single_workflow_records(params.agent, task, params.workflow_mode, params.apply)
        records.extend(task_records)
    return records


def _build_single_workflow_records(agent, task, workflow_mode, apply):
    preview = (
        task.workflow_plan
        or _try_workflow_plan(
            task.goal,
            quality_contract=task.quality_contract,
            context_manifest=task.context_manifest,
            allowed_write_roots=_workflow_extra_write_roots(task),
        )
        or {}
    )
    worker_count = len(preview.get("workers") or []) if isinstance(preview, dict) else 0

    if not apply:
        return _build_dry_run_workflow_records(
            DryRunWorkflowRecordParams(agent, task, workflow_mode, preview, worker_count)
        )

    planned = agent.subagents.ensure_workflow_plan(task.id, workflow_mode=workflow_mode)
    records = [_workflow_plan_record(agent, task, planned)]
    if workflow_mode == "auto" and planned.workflow_plan.get("ok"):
        records.append(_workflow_spawn_record(agent, task, planned))
    return records


def _build_dry_run_workflow_records(params: DryRunWorkflowRecordParams):
    agent = params.agent
    task = params.task
    preview = params.preview
    worker_count = params.worker_count
    records = [
        agent.subagents.make_dispatch_record(
            params=DispatchRecordParams(
            step="workflow",
            action="plan_workflow",
            run_id=task.id,
            dry_run=True,
            applied=False,
            ok=bool(preview),
            message=(
                f"dry-run: 将为父任务写入 workflow 计划，template="
                f"{preview.get('selected_template_id', '') or 'none'} workers={worker_count}。"
            ),
            before_status=task.status,
            after_status=task.status,
            before_verification_status=task.verification_status,
            after_verification_status=task.verification_status,
            evidence_paths=[task.task_dir],
            ),
        )
    ]
    if params.workflow_mode == "auto" and preview.get("ok"):
        records.append(
            agent.subagents.make_dispatch_record(
                params=DispatchRecordParams(
                step="workflow",
                action="spawn_workflow_workers",
                run_id=task.id,
                dry_run=True,
                applied=False,
                ok=True,
                message=f"dry-run: apply 时会根据 workflow 计划创建 {worker_count} 个 worker 子工单。",
                before_status=task.status,
                after_status=task.status,
                before_verification_status=task.verification_status,
                after_verification_status=task.verification_status,
                evidence_paths=[task.task_dir],
                ),
            )
        )
    return records


def _workflow_plan_record(agent, task, planned):
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
        step="workflow",
        action="plan_workflow",
        run_id=task.id,
        dry_run=False,
        applied=bool(planned.workflow_plan),
        ok=bool(planned.workflow_plan),
        message=_workflow_plan_message(planned),
        before_status=task.status,
        after_status=planned.status,
        before_verification_status=task.verification_status,
        after_verification_status=planned.verification_status,
        evidence_paths=[planned.task_dir],
        ),
    )


def _workflow_plan_message(planned) -> str:
    if not planned.workflow_plan:
        return "未能生成 workflow 计划。"
    worker_count = len(planned.workflow_plan.get("workers") or [])
    return f"已写入 workflow 计划，template={planned.workflow_template_id or 'none'} workers={worker_count}。"


def _workflow_spawn_record(agent, task, planned):
    before_child_count = len(planned.workflow_child_run_ids)
    planned, created_children = agent.subagents.realize_workflow_plan(task.id)
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
        step="workflow",
        action="spawn_workflow_workers",
        run_id=task.id,
        dry_run=False,
        applied=bool(created_children) or before_child_count > 0,
        ok=True,
        message=_workflow_spawn_message(created_children),
        before_status=task.status,
        after_status=planned.status,
        before_verification_status=task.verification_status,
        after_verification_status=planned.verification_status,
        evidence_paths=[planned.task_dir, *[child.task_dir for child in created_children]],
        ),
    )


def _workflow_spawn_message(created_children) -> str:
    if created_children:
        return f"已创建 {len(created_children)} 个 workflow worker 子工单。"
    return "workflow worker 子工单已存在，本轮未重复创建。"


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
