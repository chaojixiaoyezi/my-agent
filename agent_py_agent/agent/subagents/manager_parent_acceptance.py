# LLM: Manager parent-acceptance bridge; keep the mixin class thin and delegate policy details here.
# 模块用途: 承接 SubAgentManager 的父级验收 plan/write/apply 流程，避免 manager_acceptance 类继续膨胀。
from __future__ import annotations

from pathlib import Path

from .acceptance_review_service import AcceptanceReviewOptions
from .acceptance_workspace import acceptance_workspace_root_for_task
from .debug_trace import trace_acceptance_decision, trace_acceptance_next_action
from .parent_acceptance_apply import (
    ParentAcceptanceApplyResult,
    applied_parent_acceptance_apply_result,
    blocked_parent_acceptance_apply_result,
    write_parent_acceptance_apply_result_file,
)
from .parent_acceptance_auto_execution import (
    ParentAcceptanceAutoExecutionOptions,
    ParentAcceptanceAutoExecutionResult,
    build_parent_acceptance_auto_execution,
)
from .parent_acceptance_auto_followup import build_parent_acceptance_auto_followup
from .parent_acceptance_auto_policy import (
    ParentAcceptanceAutoPolicy,
    build_parent_acceptance_auto_policy,
)
from .parent_acceptance_controller import (
    ParentAcceptanceDecision,
    build_parent_acceptance_decision,
    write_parent_acceptance_decision_file,
)
from .parent_acceptance_followup_consistency import followup_consistency_block
from .parent_acceptance_followup_control import (
    BlockedFollowUpControlInput,
    ParentAcceptanceFollowUpControlOptions,
    ParentAcceptanceFollowUpControlResult,
    acceptance_followup_control_result,
    blocked_followup_control_result,
    followup_action,
    followup_command_for_action,
    followup_status,
    invalid_followup_control_result,
    invalid_followup_error,
    parent_acceptance_followup_ref,
    preview_parent_acceptance_followup_control,
    read_parent_acceptance_followup_payload,
    rescue_followup_control_result,
    write_parent_acceptance_followup_control_file,
)
from .parent_acceptance_next_action import (
    ParentAcceptanceNextAction,
    build_parent_acceptance_next_action,
)
from .parent_acceptance_rescue_followup import (
    missing_followup_rescue_payload,
    missing_followup_rescue_preview,
)
from .reports import ActionPlanItem
from .services.action_options import ActionApplyOptions


# LLM: manager_plan_parent_acceptance is side-effect free and only reads task-local fact sources.
# 函数用途: 给 manager 方法提供父级验收 dry-run 决策实现；不执行 tests、不改任务状态。
def manager_plan_parent_acceptance(manager, run_id: str) -> ParentAcceptanceDecision:
    task = manager.load(run_id)
    decision = build_parent_acceptance_decision(
        task,
        workspace_root=acceptance_workspace_root(manager, task),
    )
    return trace_acceptance_decision(manager, task, decision)


# LLM: manager_write_parent_acceptance_decision persists only the dry-run decision audit file.
# 函数用途: 写入 `parent_acceptance_decision.json`，保持 refs-only，不应用决策。
def manager_write_parent_acceptance_decision(manager, run_id: str) -> ParentAcceptanceDecision:
    task = manager.load(run_id)
    decision = build_parent_acceptance_decision(
        task,
        workspace_root=acceptance_workspace_root(manager, task),
    )
    write_parent_acceptance_decision_file(task, decision)
    return trace_acceptance_decision(manager, task, decision)


# LLM: manager_apply_parent_acceptance_decision only bridges inspect_only into normal acceptance apply.
# 函数用途: 显式应用父级验收决策；非 inspect_only 决策只写拦截审计，等待后续显式动作。
def manager_apply_parent_acceptance_decision(
    manager,
    run_id: str,
    *,
    reviewer: str = "parent",
    note: str = "",
) -> ParentAcceptanceApplyResult:
    task = manager.load(run_id)
    decision = build_parent_acceptance_decision(
        task,
        workspace_root=acceptance_workspace_root(manager, task),
    )
    decision_ref = str(write_parent_acceptance_decision_file(task, decision))
    if decision.decision != "inspect_only":
        result = blocked_parent_acceptance_apply_result(task, decision, decision_ref=decision_ref)
        write_parent_acceptance_apply_result_file(task, result)
        return result

    record = manager.review_acceptance(
        run_id,
        options=AcceptanceReviewOptions(apply=True, reviewer=reviewer, note=note, execute_tests=False),
    )
    manager._write_acceptance_record_files(record)
    manager._index_acceptance_review(record)
    manager._append_acceptance_review_log(record)
    result = applied_parent_acceptance_apply_result(task, decision, record, decision_ref=decision_ref)
    write_parent_acceptance_apply_result_file(task, result)
    return result


# LLM: manager_plan_parent_acceptance_next_action exposes the scheduler-facing dry-run recommendation.
# 函数用途: 读取父级验收事实源和审计引用，返回下一步显式动作建议；不执行命令、不写状态。
def manager_plan_parent_acceptance_next_action(manager, run_id: str) -> ParentAcceptanceNextAction:
    task = manager.load(run_id)
    action = build_parent_acceptance_next_action(
        task,
        workspace_root=acceptance_workspace_root(manager, task),
    )
    return trace_acceptance_next_action(manager, task, action)


# LLM: manager_plan_parent_acceptance_auto_policy exposes dry-run policy gating for schedulers.
# 函数用途: 读取 next-action 并写入自动策略审计；不执行建议动作、不修改任务状态。
def manager_plan_parent_acceptance_auto_policy(manager, run_id: str) -> ParentAcceptanceAutoPolicy:
    task = manager.load(run_id)
    return build_parent_acceptance_auto_policy(
        task,
        workspace_root=acceptance_workspace_root(manager, task),
    )


# LLM: manager_plan_parent_acceptance_auto_execution exposes the facade and only executes tests on explicit confirmation.
# 函数用途: 生成父级验收自动执行计划；只有 execute_tests 显式为 true 时才执行 tests，不 apply、不 rescue、不改状态。
def manager_plan_parent_acceptance_auto_execution(
    manager,
    run_id: str,
    *,
    options: ParentAcceptanceAutoExecutionOptions | None = None,
) -> ParentAcceptanceAutoExecutionResult:
    task = manager.load(run_id)
    return build_parent_acceptance_auto_execution(
        task,
        workspace_root=acceptance_workspace_root(manager, task),
        options=options,
    )


# LLM: manager_plan_parent_acceptance_followup must preview the same consistency gate that apply uses.
# 函数用途: 读取测试后的 follow-up 审计并校验当前任务状态；只返回下一步建议，不写文件、不改状态。
def manager_plan_parent_acceptance_followup(
    manager,
    run_id: str,
) -> ParentAcceptanceFollowUpControlResult:
    task = manager.load(run_id)
    _ensure_followup_from_current_tests(task, workspace_root=acceptance_workspace_root(manager, task))
    rescue = missing_followup_rescue_preview(task, workspace_root=acceptance_workspace_root(manager, task))
    if rescue is not None:
        return rescue
    payload = read_parent_acceptance_followup_payload(task)
    status = followup_status(payload)
    action = followup_action(payload)
    consistency = followup_consistency_block(task, payload, status, action) if status else None
    if consistency is not None:
        return consistency
    return preview_parent_acceptance_followup_control(task)


# LLM: manager_apply_parent_acceptance_followup is the controlled manual gate after tests.
# 函数用途: 显式处理 follow-up；测试通过才 apply 验收，救援必须走 action handler 接管门。
def manager_apply_parent_acceptance_followup(
    manager,
    run_id: str,
    *,
    options: ParentAcceptanceFollowUpControlOptions | None = None,
) -> ParentAcceptanceFollowUpControlResult:
    task = manager.load(run_id)
    opts = options or ParentAcceptanceFollowUpControlOptions()
    _ensure_followup_from_current_tests(task, workspace_root=acceptance_workspace_root(manager, task))
    payload = read_parent_acceptance_followup_payload(task)
    if not payload:
        payload = missing_followup_rescue_payload(
            task,
            workspace_root=acceptance_workspace_root(manager, task),
        )
    result = _followup_control_result(manager, task, payload, opts)
    write_parent_acceptance_followup_control_file(task, result)
    return result


# LLM: _ensure_followup_from_current_tests lets plain subagents-tests reports feed the same manual gate.
# 函数用途: 当已有 test_execution.json 但缺 follow-up 审计时，生成 refs-only follow-up；不执行 tests、不改状态。
def _ensure_followup_from_current_tests(task, *, workspace_root: Path) -> None:
    followup_path = parent_acceptance_followup_ref(task)
    if followup_path.exists():
        return
    report_path = Path(task.reports_dir) / "test_execution.json"
    if not report_path.exists():
        return
    build_parent_acceptance_auto_followup(
        task,
        workspace_root=workspace_root,
        execution_ref="",
        test_execution_ref=str(report_path),
    )

# LLM: _followup_control_result routes stale apply follow-ups into explicit rescue only through the action gate.
# 函数用途: 根据 follow-up action 和一致性结果选择 apply、rescue 或 blocked；不猜测缺失事实。
def _followup_control_result(
    manager,
    task,
    payload: dict,
    opts: ParentAcceptanceFollowUpControlOptions,
) -> ParentAcceptanceFollowUpControlResult:
    invalid = invalid_followup_error(payload)
    if invalid:
        return invalid_followup_control_result(task, invalid)
    status = followup_status(payload)
    action = followup_action(payload)
    if not status:
        return blocked_followup_control_result(
            task,
            BlockedFollowUpControlInput(
                status="missing_followup",
                action="run_tests",
                message="parent acceptance follow-up is missing; run explicit tests first",
                recommended_command=followup_command_for_action(task.id, "run_tests"),
            ),
        )
    if not opts.apply:
        return preview_parent_acceptance_followup_control(task)
    consistency = followup_consistency_block(task, payload, status, action)
    if consistency is not None:
        if consistency.status == "needs_manual_rescue" and consistency.action == "plan_rescue":
            return _apply_followup_rescue(manager, task, opts)
        return consistency
    if action == "apply_acceptance" and status == "ready_for_manual_apply":
        return _apply_followup_acceptance(manager, task, opts)
    if action == "plan_rescue" and status == "needs_manual_rescue":
        return _apply_followup_rescue(manager, task, opts)
    return blocked_followup_control_result(
        task,
        BlockedFollowUpControlInput(
            status=status,
            action=action,
            message=f"follow-up action {action or status} is not safe for explicit apply",
            recommended_command=followup_command_for_action(task.id, action),
        ),
    )

# LLM: _apply_followup_acceptance reuses the existing inspect_only acceptance apply bridge.
# 函数用途: 测试通过后显式应用父级验收；实际状态写回仍由既有 apply 决策函数负责。
def _apply_followup_acceptance(
    manager,
    task,
    opts: ParentAcceptanceFollowUpControlOptions,
) -> ParentAcceptanceFollowUpControlResult:
    apply_result = manager_apply_parent_acceptance_decision(
        manager,
        task.id,
        reviewer=opts.reviewer,
        note=opts.note,
    )
    return acceptance_followup_control_result(manager.load(task.id), apply_result)


# LLM: _apply_followup_rescue reuses action-apply takeover gates instead of direct takeover writes.
# 函数用途: 测试失败后显式接管；缺少 take_over_by 会被 action handler 拦截，不直接改状态。
def _apply_followup_rescue(
    manager,
    task,
    opts: ParentAcceptanceFollowUpControlOptions,
) -> ParentAcceptanceFollowUpControlResult:
    action = _followup_rescue_action(task)
    record = manager._apply_action_item(
        action,
        options=ActionApplyOptions(
            apply=True,
            action_filter="takeover_or_reassign",
            run_id=task.id,
            take_over_by=opts.take_over_by,
            locked_files=list(opts.locked_files),
            limit=1,
        ),
    )
    manager._append_action_apply_log(record)
    return rescue_followup_control_result(manager.load(task.id), record)


# LLM: _followup_rescue_action builds the explicit action item consumed by the existing rescue handler.
# 函数用途: 构造 takeover_or_reassign 动作，复用 action handler 的通道检查和审计字段。
def _followup_rescue_action(task) -> ActionPlanItem:
    return ActionPlanItem(
        id=f"parent-followup-rescue-{task.id}",
        run_id=task.id,
        severity="HIGH",
        priority=950,
        action="takeover_or_reassign",
        reason="parent acceptance tests failed; manual rescue requested from follow-up",
        source_issue_kinds=["parent_acceptance_test_failed"],
        suggested_commands=[followup_command_for_action(task.id, "plan_rescue")],
        would_change_status_to="TAKEN_OVER",
        rescue_trigger="parent_acceptance_test_failed",
        rescue_strategy="manual_takeover_after_failed_parent_tests",
        escalation_target="parent",
        rescue_context_refs=[str(Path(task.reports_dir) / "parent_acceptance_auto_followup.json")],
        rescue_packet={
            "auto_retry": False,
            "requires_manual_confirmation": True,
            "source": "parent_acceptance_followup",
        },
        requires_confirmation=True,
        dry_run=False,
        owner=task.owner,
        final_owner=task.final_owner,
        task_dir=task.task_dir,
    )


# LLM: acceptance_workspace_root mirrors real execution and prefers the root containing task artifacts.
# 函数用途: 推断父级验收命令所在项目根目录；多工作区时选择包含当前产物的 root，避免扫错项目。
def acceptance_workspace_root(manager, task=None) -> Path:
    scoped = acceptance_workspace_root_for_task(manager, task=task)
    if scoped:
        return scoped
    workspace_root = getattr(manager, "workspace_root", None)
    if workspace_root:
        return Path(workspace_root).resolve()
    workspace = Path(manager.workspace)
    if workspace.name == "subagents" and workspace.parent.name == ".my_agent":
        return workspace.parent.parent
    return workspace.parent if workspace.parent != workspace else workspace
