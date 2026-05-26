# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagents.models import (
    SubAgentCapabilityRouteOptions,
    SubAgentDueCheckOptions,
    SubAgentLeadershipRecoveryPlanOptions,
)
from ..subagents.services.action_options import ActionApplyOptions
from ..subagents.services.dispatch_params import DispatchRecordParams, DispatchWatchRecordParams
from .dispatch_record_params import (
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


# LLM: DueCheckRecordParams bundles dispatch due-check scoping and apply mode for a single record.
# 类用途: 保存 due-check dispatch record 构造参数，避免函数签名随着 root/run 过滤继续膨胀。
@dataclass(frozen=True)
class DueCheckRecordParams:
    agent: Any
    cfg: CapabilityConfig
    apply: bool
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None


# ---------------------------------------------------------------------------
# Step record builders
# ---------------------------------------------------------------------------


# LLM: make_due_check_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建到期检查记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def make_due_check_record(params: DueCheckRecordParams):
    agent = params.agent
    options = SubAgentDueCheckOptions(
        config=params.cfg,
        write_report=params.apply,
        root_id=params.root_id,
        include_run_ids=list(params.include_run_ids or []),
        exclude_run_ids=list(params.exclude_run_ids or []),
    )
    due_report = (
        agent.subagents.write_due_check(params=options)
        if params.apply
        else agent.subagents.due_check(params=options)
    )
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
        step="due_check",
        action="scan",
        dry_run=not params.apply,
        applied=False,
        ok=True,
        message=f"发现 {due_report.summary.get('total', 0)} 个 due-check issue。",
        evidence_paths=[str(agent.subagents.workspace / "subagent_due_check.json")],
        ),
    )


# LLM: make_leadership_recovery_plan_record surfaces stale-coordinator handoff plans without mutating state.
# 函数用途: 在 dispatch/watch 中写出批量领导权恢复计划 ref，只做 inspect_refs，不重挂任务树。
def make_leadership_recovery_plan_record(agent, cfg):
    options = SubAgentLeadershipRecoveryPlanOptions(config=cfg, write_report=True)
    plan = agent.subagents.write_leadership_recovery_plan(params=options)
    affected = plan.summary.get("stale_coordinators", 0) + plan.summary.get("failed_parent_nodes", 0)
    if affected <= 0:
        return None
    plan_ref = str(agent.subagents.workspace / "subagent_leadership_recovery_plan.json")
    return agent.subagents.make_dispatch_record(
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


# LLM: make_action_apply_records 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建动作应用记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def make_action_apply_records(params: ActionApplyRecordParams):
    agent = params.agent
    records = []
    action_options = _action_apply_options(params)
    action_report = _action_apply_report(agent, params, action_options)
    for item in action_report.records:
        records.append(_action_apply_dispatch_record(agent, item))
    return records


# LLM: _action_apply_options mirrors dispatch scoping into the subagent action apply bundle.
# 函数用途: 从 dispatch 参数构造 ActionApplyOptions，保证 apply/dry-run 两条路径使用同一过滤条件。
def _action_apply_options(params: ActionApplyRecordParams) -> ActionApplyOptions:
    return ActionApplyOptions(
        apply=params.apply,
        take_over_by=params.take_over_by or "",
        locked_files=params.locked_files or [],
        limit=params.limit,
        root_id=params.root_id,
        include_run_ids=list(params.include_run_ids or []),
        exclude_run_ids=list(params.exclude_run_ids or []),
    )


# LLM: _action_apply_report calls the mutating or dry-run action path with the same options object.
# 函数用途: 根据 apply 开关选择 write_action_apply_report 或 apply_actions。
def _action_apply_report(agent: Any, params: ActionApplyRecordParams, options: ActionApplyOptions):
    if params.apply:
        return agent.subagents.write_action_apply_report(params.cfg, options=options)
    return agent.subagents.apply_actions(params.cfg, options=options)


# LLM: _action_apply_dispatch_record projects one action apply record into the dispatch ledger schema.
# 函数用途: 把 action apply report 的单条记录转换为 dispatch record。
def _action_apply_dispatch_record(agent: Any, item):
    return agent.subagents.make_dispatch_record(
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


# ---------------------------------------------------------------------------
# Pending work state management
# ---------------------------------------------------------------------------


# LLM: update_pending_work_state 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新pendingwork状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新运行循环、工具调用、调度记录和最终响应，需避免破坏既有状态机约定。
def update_pending_work_state(agent) -> bool:
    from .runner_dispatch import (
        _dispatch_runner_candidates,
        _runner_max_attempts,
        _same_run_redispatch_limit,
    )

    runner_max_attempts = _runner_max_attempts(agent.config.runner_failure_policy)
    same_run_limit = _same_run_redispatch_limit(getattr(agent.config, "same_run_redispatch_limit", None))
    candidates = _dispatch_runner_candidates(
        agent.subagents.list_runs(),
        max_runners=999,
        runner_max_attempts=runner_max_attempts,
        same_run_redispatch_limit=same_run_limit,
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
