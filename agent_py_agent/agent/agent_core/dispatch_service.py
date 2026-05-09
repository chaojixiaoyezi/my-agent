# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagents.acceptance_review_service import AcceptanceReviewOptions
from ..subagents.models import SubAgentCapabilityRouteOptions, SubAgentDueCheckOptions
from ..subagents.parent_acceptance_auto_execution import ParentAcceptanceAutoExecutionOptions
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


# LLM: MakeDispatchWatchRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存make调度监控记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: make_due_check_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建到期检查记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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


# LLM: make_action_apply_records 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建动作应用记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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


# LLM: make_capability_route_records 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建能力route记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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


# LLM: make_patch_review_records 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建补丁审查记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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


# LLM: make_acceptance_records 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建验收记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def make_acceptance_records(params: AcceptanceRecordParams):
    agent = params.agent
    records = []
    apply_acceptance = params.apply and not params.execute_acceptance_tests
    options = AcceptanceReviewOptions(
        apply=apply_acceptance,
        reviewer=params.reviewer,
        note=params.note,
        limit=params.limit,
    )
    acceptance_report = (
        agent.subagents.write_acceptance_review_report(
            options=options,
        )
        if params.apply
        else agent.subagents.review_acceptances(
            options=options,
        )
    )
    for item in acceptance_report.records:
        policy_summary = _parent_acceptance_policy_summary(
            agent,
            item.run_id,
            options=_auto_execution_options(params.execute_acceptance_tests),
        )
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
                **policy_summary,
                ),
            )
        )
    return records


# LLM: _parent_acceptance_policy_summary attaches policy/execution refs and runs tests only with explicit options.
# 函数用途: 为 acceptance 调度记录生成自动验收策略和执行摘要；默认只写审计，显式 options 才跑 tests，始终不改任务状态。
def _parent_acceptance_policy_summary(
    agent,
    run_id: str,
    *,
    options: ParentAcceptanceAutoExecutionOptions | None = None,
) -> dict[str, object]:
    if not run_id:
        return {}
    task = agent.subagents.load(run_id)
    policy = agent.subagents.plan_parent_acceptance_auto_policy(run_id)
    execution = agent.subagents.plan_parent_acceptance_auto_execution(run_id, options=options)
    policy_ref = Path(task.reports_dir) / "parent_acceptance_auto_policy.json"
    execution_ref = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
    return {
        "parent_acceptance_policy_ref": str(policy_ref),
        "parent_acceptance_policy_decision": policy.decision,
        "parent_acceptance_policy_action": policy.action,
        "parent_acceptance_policy_would_execute": bool(policy.would_execute),
        "parent_acceptance_policy_executed": bool(policy.executed),
        "parent_acceptance_policy_execution_mode": policy.execution_mode,
        "parent_acceptance_policy_automatic_execution_allowed": bool(
            policy.automatic_execution_allowed
        ),
        "parent_acceptance_policy_recommended_command": policy.recommended_command,
        "parent_acceptance_policy_preflight_status": policy.preflight_status,
        "parent_acceptance_policy_ready_for_automatic_execution": bool(
            policy.ready_for_automatic_execution
        ),
        "parent_acceptance_policy_preflight_blockers": list(policy.preflight_blockers),
        "parent_acceptance_auto_execution_ref": str(execution_ref),
        "parent_acceptance_auto_execution_status": execution.status,
        "parent_acceptance_auto_execution_allowed": bool(execution.execution_allowed),
        "parent_acceptance_auto_execution_executed": bool(execution.executed),
        "parent_acceptance_auto_execution_guard_status": execution.guard_status,
        "parent_acceptance_auto_execution_blocked_by": list(execution.blocked_by),
        "parent_acceptance_auto_execution_test_ref": execution.test_execution_ref,
        "parent_acceptance_auto_execution_test_total": execution.test_total,
        "parent_acceptance_auto_execution_test_failed": execution.test_failed,
        "parent_acceptance_followup_ref": execution.followup_ref,
        "parent_acceptance_followup_status": execution.followup_status,
        "parent_acceptance_followup_action": execution.followup_action,
        "parent_acceptance_followup_command": execution.followup_command,
        "parent_acceptance_followup_reason": execution.followup_reason,
    }


# LLM: _auto_execution_options converts the dispatch flag into the guarded parent acceptance options bundle.
# 函数用途: 只在 dispatch/watch 显式确认时创建 execute_tests 选项包；默认保持 dry-run facade。
def _auto_execution_options(execute_tests: bool) -> ParentAcceptanceAutoExecutionOptions | None:
    if not execute_tests:
        return None
    return ParentAcceptanceAutoExecutionOptions(execute_tests=True)


# ---------------------------------------------------------------------------
# Pending work state management
# ---------------------------------------------------------------------------


# LLM: update_pending_work_state 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新pendingwork状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新运行循环、工具调用、调度记录和最终响应，需避免破坏既有状态机约定。
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


# LLM: make_dispatch_watch_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建make调度监控记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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
