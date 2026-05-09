# LLM: Manager parent-acceptance bridge; keep the mixin class thin and delegate policy details here.
# 模块用途: 承接 SubAgentManager 的父级验收 plan/write/apply 流程，避免 manager_acceptance 类继续膨胀。
from __future__ import annotations

from pathlib import Path

from .acceptance_review_service import AcceptanceReviewOptions
from .parent_acceptance_apply import (
    ParentAcceptanceApplyResult,
    applied_parent_acceptance_apply_result,
    blocked_parent_acceptance_apply_result,
    write_parent_acceptance_apply_result_file,
)
from .parent_acceptance_auto_execution import (
    ParentAcceptanceAutoExecutionResult,
    build_parent_acceptance_auto_execution,
)
from .parent_acceptance_auto_policy import (
    ParentAcceptanceAutoPolicy,
    build_parent_acceptance_auto_policy,
)
from .parent_acceptance_controller import (
    ParentAcceptanceDecision,
    build_parent_acceptance_decision,
    write_parent_acceptance_decision_file,
)
from .parent_acceptance_next_action import (
    ParentAcceptanceNextAction,
    build_parent_acceptance_next_action,
)


# LLM: manager_plan_parent_acceptance is side-effect free and only reads task-local fact sources.
# 函数用途: 给 manager 方法提供父级验收 dry-run 决策实现；不执行 tests、不改任务状态。
def manager_plan_parent_acceptance(manager, run_id: str) -> ParentAcceptanceDecision:
    task = manager.load(run_id)
    return build_parent_acceptance_decision(
        task,
        workspace_root=acceptance_workspace_root(manager),
    )


# LLM: manager_write_parent_acceptance_decision persists only the dry-run decision audit file.
# 函数用途: 写入 `parent_acceptance_decision.json`，保持 refs-only，不应用决策。
def manager_write_parent_acceptance_decision(manager, run_id: str) -> ParentAcceptanceDecision:
    task = manager.load(run_id)
    decision = build_parent_acceptance_decision(
        task,
        workspace_root=acceptance_workspace_root(manager),
    )
    write_parent_acceptance_decision_file(task, decision)
    return decision


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
        workspace_root=acceptance_workspace_root(manager),
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
    return build_parent_acceptance_next_action(
        task,
        workspace_root=acceptance_workspace_root(manager),
    )


# LLM: manager_plan_parent_acceptance_auto_policy exposes dry-run policy gating for schedulers.
# 函数用途: 读取 next-action 并写入自动策略审计；不执行建议动作、不修改任务状态。
def manager_plan_parent_acceptance_auto_policy(manager, run_id: str) -> ParentAcceptanceAutoPolicy:
    task = manager.load(run_id)
    return build_parent_acceptance_auto_policy(
        task,
        workspace_root=acceptance_workspace_root(manager),
    )


# LLM: manager_plan_parent_acceptance_auto_execution exposes the dry-run executor facade.
# 函数用途: 生成父级验收自动执行计划和审计文件；不启动命令、不修改任务状态。
def manager_plan_parent_acceptance_auto_execution(
    manager,
    run_id: str,
) -> ParentAcceptanceAutoExecutionResult:
    task = manager.load(run_id)
    return build_parent_acceptance_auto_execution(
        task,
        workspace_root=acceptance_workspace_root(manager),
    )


# LLM: acceptance_workspace_root mirrors real-test execution workspace inference for dry-run command preflight.
# 函数用途: 推断父级验收命令所在项目根目录；这里只用于风险预检，不会启动进程。
def acceptance_workspace_root(manager) -> Path:
    workspace = Path(manager.workspace)
    if workspace.name == "subagents" and workspace.parent.name == ".my_agent":
        return workspace.parent.parent
    return workspace.parent if workspace.parent != workspace else workspace
