from __future__ import annotations

from ..subagents.services.dispatch_params import DispatchRecordParams
from ..subagents.services.workflow import _try_workflow_plan, _workflow_extra_write_roots
from .dispatch_record_params import DryRunWorkflowRecordParams, WorkflowRecordParams


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
