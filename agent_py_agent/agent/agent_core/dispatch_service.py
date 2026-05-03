"""LLM: dispatch orchestration, step sequencing, and workflow handling.

给人看的解释：
负责父代理调度的核心编排，按顺序执行各阶段：planner、workflow、due-check、
action_apply、capability_route、runner、patch_review、acceptance。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig

if TYPE_CHECKING:
    from ..core import SimpleAgent


# ---------------------------------------------------------------------------
# Workflow step helpers
# ---------------------------------------------------------------------------


def build_workflow_records(agent, tasks, workflow_mode, limit, apply):
    """Build dispatch records for workflow planning."""
    records = []
    workflow_candidates = [
        task
        for task in tasks
        if not task.parent_id
        and not task.workflow_parent_run_id
        and task.status not in {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
    ]
    if limit > 0:
        workflow_candidates = workflow_candidates[:limit]
    for task in workflow_candidates:
        preview = task.workflow_plan or agent.subagents._try_workflow_plan(
            task.goal,
            quality_contract=task.quality_contract,
            context_manifest=task.context_manifest,
            allowed_write_roots=agent.subagents._workflow_extra_write_roots(task),
        ) or {}
        worker_count = len(preview.get("workers") or []) if isinstance(preview, dict) else 0
        if not apply:
            records.append(
                agent.subagents.make_dispatch_record(
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
                )
            )
            if workflow_mode == "auto" and preview.get("ok"):
                records.append(
                    agent.subagents.make_dispatch_record(
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
                    )
                )
            continue

        planned = agent.subagents.ensure_workflow_plan(task.id, workflow_mode=workflow_mode)
        records.append(
            agent.subagents.make_dispatch_record(
                step="workflow",
                action="plan_workflow",
                run_id=task.id,
                dry_run=False,
                applied=bool(planned.workflow_plan),
                ok=bool(planned.workflow_plan),
                message=(
                    f"已写入 workflow 计划，template={planned.workflow_template_id or 'none'} "
                    f"workers={len(planned.workflow_plan.get('workers') or []) if planned.workflow_plan else 0}。"
                    if planned.workflow_plan
                    else "未能生成 workflow 计划。"
                ),
                before_status=task.status,
                after_status=planned.status,
                before_verification_status=task.verification_status,
                after_verification_status=planned.verification_status,
                evidence_paths=[planned.task_dir],
            )
        )
        if workflow_mode == "auto" and planned.workflow_plan.get("ok"):
            before_child_count = len(planned.workflow_child_run_ids)
            planned, created_children = agent.subagents.realize_workflow_plan(task.id)
            records.append(
                agent.subagents.make_dispatch_record(
                    step="workflow",
                    action="spawn_workflow_workers",
                    run_id=task.id,
                    dry_run=False,
                    applied=bool(created_children) or before_child_count > 0,
                    ok=True,
                    message=(
                        f"已创建 {len(created_children)} 个 workflow worker 子工单。"
                        if created_children
                        else "workflow worker 子工单已存在，本轮未重复创建。"
                    ),
                    before_status=task.status,
                    after_status=planned.status,
                    before_verification_status=task.verification_status,
                    after_verification_status=planned.verification_status,
                    evidence_paths=[planned.task_dir, *[child.task_dir for child in created_children]],
                )
            )
    return records


# ---------------------------------------------------------------------------
# Step record builders
# ---------------------------------------------------------------------------


def make_due_check_record(agent, cfg, apply):
    """Create due-check dispatch record."""
    due_report = agent.subagents.write_due_check(cfg) if apply else agent.subagents.due_check(cfg)
    return agent.subagents.make_dispatch_record(
        step="due_check",
        action="scan",
        dry_run=not apply,
        applied=False,
        ok=True,
        message=f"发现 {due_report.summary.get('total', 0)} 个 due-check issue。",
        evidence_paths=[str(agent.subagents.workspace / "subagent_due_check.json")],
    )


def make_action_apply_records(agent, cfg, apply, take_over_by, locked_files, limit):
    """Create action_apply dispatch records."""
    records = []
    action_report = (
        agent.subagents.write_action_apply_report(
            cfg,
            apply=apply,
            take_over_by=take_over_by,
            locked_files=locked_files or [],
            limit=limit,
        )
        if apply
        else agent.subagents.apply_actions(
            cfg,
            apply=False,
            take_over_by=take_over_by,
            locked_files=locked_files or [],
            limit=limit,
        )
    )
    for item in action_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
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
            )
        )
    return records


def make_capability_route_records(agent, router, cfg, apply, limit):
    """Create capability_route dispatch records."""
    records = []
    route_report = (
        agent.subagents.write_capability_route_report(
            router,
            cfg,
            apply=apply,
            limit=limit,
        )
        if apply
        else agent.subagents.route_capability_requests(
            router,
            cfg,
            apply=False,
            limit=limit,
        )
    )
    for item in route_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
                step="capability_route",
                action=item.status.lower(),
                run_id=item.run_id,
                dry_run=item.dry_run,
                applied=not item.dry_run,
                ok=item.status in {"WOULD_GRANT", "GRANTED"},
                message=item.message,
                evidence_paths=[str(agent.subagents.workspace / "subagent_capability_route_report.json")],
            )
        )
    return records


def make_patch_review_records(agent, patch_run_ids, apply, reviewer, note, limit):
    """Create patch_review dispatch records."""
    if not patch_run_ids:
        return []
    records = []
    patch_report = (
        agent.subagents.write_patch_review_report(
            patch_run_ids,
            apply=True,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        if apply
        else agent.subagents.review_patches(
            patch_run_ids,
            apply=False,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
    )
    for item in patch_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
                step="patch_review",
                action=item.decision.lower(),
                run_id=item.run_id,
                dry_run=item.dry_run,
                applied=item.applied,
                ok=item.ok,
                message=item.message,
                evidence_paths=item.evidence_paths,
            )
        )
    return records


def make_acceptance_records(agent, apply, reviewer, note, limit):
    """Create acceptance dispatch records."""
    records = []
    acceptance_report = (
        agent.subagents.write_acceptance_review_report(
            apply=True,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        if apply
        else agent.subagents.review_acceptances(
            apply=False,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
    )
    for item in acceptance_report.records:
        records.append(
            agent.subagents.make_dispatch_record(
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
            )
        )
    return records


# ---------------------------------------------------------------------------
# Pending work state management
# ---------------------------------------------------------------------------


def update_pending_work_state(agent) -> bool:
    """Update _has_pending_work by checking for dispatchable runner candidates."""
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


def make_dispatch_watch_record(agent, cycle, dry_run, ok, message, dispatch_record_count, dispatch_summary, started_at, ended_at, evidence_paths):
    """Create a dispatch watch heartbeat record."""
    return agent.subagents.make_dispatch_watch_record(
        cycle=cycle,
        dry_run=dry_run,
        ok=ok,
        message=message,
        dispatch_record_count=dispatch_record_count,
        dispatch_summary=dispatch_summary,
        started_at=started_at,
        ended_at=ended_at,
        evidence_paths=evidence_paths,
    )


def check_watch_stopping(cycle, max_cycles, stop_path, max_consecutive_rounds, consecutive_rounds, lock_path, lock):
    """Check if watch loop should stop."""
    more_cycles = max_cycles == 0 or cycle < max_cycles
    stop_requested = bool(stop_path and stop_path.exists())
    if stop_requested:
        more_cycles = False
    return more_cycles, stop_requested