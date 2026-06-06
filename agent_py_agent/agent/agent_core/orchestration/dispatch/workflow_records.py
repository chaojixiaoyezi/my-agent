
from __future__ import annotations

from ....subagents.services.dispatch.params import DispatchRecordParams
from ....subagents.services.workflow import (
    _try_workflow_plan,
    _workflow_attr,
    _workflow_attr_text,
    _workflow_extra_write_roots,
    _WorkflowPlanAttempt,
)


def build_workflow_records(
    agent,
    ctx,
    tasks: list,
    *,
    override_task_off: bool = False,
):
    workflow_candidates = [
        task
        for task in tasks
        if not task.parent_id
        and not task.workflow_parent_run_id
        and _task_allows_dispatch_workflow(task, override_task_off)
        and task.status not in {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
    ]
    if ctx.limit > 0:
        workflow_candidates = workflow_candidates[: ctx.limit]
    records = []
    for task in workflow_candidates:
        task_records = _build_single_workflow_records(
            agent,
            task,
            ctx.normalized_workflow_mode,
            ctx.mutate_state,
        )
        records.extend(task_records)
    return records


def _task_allows_dispatch_workflow(task, override_task_off: bool = False) -> bool:
    if override_task_off:
        return True
    return str(getattr(task, "workflow_mode", "") or "").strip().lower() != "off"


def _build_single_workflow_records(agent, task, workflow_mode, apply):
    preview = (
        task.workflow_plan
        or _try_workflow_plan(_WorkflowPlanAttempt(
            goal=task.goal,
            explicit_template_id=_workflow_attr_text(task, "workflow_template_id"),
            workflow_task_type=_workflow_attr_text(task, "workflow_task_type"),
            workflow_risk_tags=_workflow_attr(task, "workflow_risk_tags"),
            quality_contract=task.quality_contract,
            context_manifest=task.context_manifest,
            allowed_write_roots=_workflow_extra_write_roots(task),
        ))
        or {}
    )
    if not apply:
        return _build_dry_run_workflow_records(agent, task, workflow_mode, preview)

    planned = agent.subagents.workflow.plan_workflow(task.id, workflow_mode=workflow_mode)
    records = [_workflow_plan_record(agent, task, planned)]
    if workflow_mode == "auto" and planned.workflow_plan.get("ok"):
        records.append(_workflow_spawn_record(agent, task, planned))
    return records


def _build_dry_run_workflow_records(agent, task, workflow_mode, preview):
    worker_count = len(preview.get("workers") or []) if isinstance(preview, dict) else 0
    records = [
        agent.subagents.dispatch.make_dispatch_record(
            params=DispatchRecordParams(
                step="workflow",
                action="plan_workflow",
                run_id=task.id,
                dry_run=True,
                applied=False,
                ok=_workflow_plan_ok(preview),
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
    if workflow_mode == "auto" and preview.get("ok"):
        records.append(
            agent.subagents.dispatch.make_dispatch_record(
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
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="workflow",
            action="plan_workflow",
            run_id=task.id,
            dry_run=False,
            applied=bool(planned.workflow_plan),
            ok=_workflow_plan_ok(planned.workflow_plan),
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
    if planned.workflow_plan.get("planning_error"):
        return "workflow 规划器失败；已保存结构化 planning_error，父代理可改为手动拆分或稍后重试。"
    worker_count = len(planned.workflow_plan.get("workers") or [])
    return f"已写入 workflow 计划，template={planned.workflow_template_id or 'none'} workers={worker_count}。"


def _workflow_plan_ok(plan: object) -> bool:
    return isinstance(plan, dict) and bool(plan) and plan.get("ok") is not False


def _workflow_spawn_record(agent, task, planned):
    before_child_count = len(planned.workflow_child_run_ids)
    planned, created_children = agent.subagents.workflow.realize_workflow_plan(task.id)
    return agent.subagents.dispatch.make_dispatch_record(
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
