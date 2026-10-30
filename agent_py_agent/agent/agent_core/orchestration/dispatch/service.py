# LLM: 派工各阶段的记录组装与待跑判断；待跑判断与正式派工共用同一候选闸和重跑次数配置，不另写规则。
# 模块用途: 为一次派工生成到期检查、动作执行、能力路由、补丁审阅等记录，并判断是否还有待跑工作。
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_py_agent.agent.subagents.reports import DispatchWatchRecord

from typing import Any

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ....subagents.models import (
    SubAgentCapabilityRouteOptions,
    SubAgentDueCheckOptions,
    SubAgentLeadershipRecoveryPlanOptions,
)
from ....subagents.services.actions import ActionApplyOptions
from ....subagents.services.dispatch.params import DispatchRecordParams, DispatchWatchRecordParams

if TYPE_CHECKING:
    from agent_py_agent.agent.core import SimpleAgent

_CAPABILITY_ROUTE_ACTIONS = {
    "WOULD_GRANT": "would_grant",
    "GRANTED": "granted",
    "WOULD_GAP": "would_gap",
    "GAP": "gap",
    "PARENT_RESOLUTION_REQUIRED": "parent_resolution_required",
}
_PATCH_REVIEW_ACTIONS = {
    "APPROVE": "approve",
    "REJECT": "reject",
    "NO_PATCHES": "no_patches",
}


# ---------------------------------------------------------------------------
# Step record builders
# ---------------------------------------------------------------------------


# LLM: 按上下文根节点和包含/排除 ID 检查到期问题；仅 mutate_state 为真时写检查报告，记录本身不代表已执行修复。
# 函数用途: 为单轮派工生成到期扫描结果及报告路径引用。
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


# LLM: 总是请求写出领导权恢复计划，但不执行接管；没有受影响父节点时返回 None，否则返回仅检查引用的记录。
# 函数用途: 向派工阶段暴露失效协调者或父节点的恢复计划证据。
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


# LLM: 从上下文构造动作选项并委托动作服务，再逐项转换实际报告；状态变更与报告写入遵循 mutate_state。
# 函数用途: 将本轮子代理动作处理结果汇总为统一派工记录列表。
def make_action_apply_records(agent: Any, ctx: Any):
    records = []
    action_options = _action_apply_options(ctx)
    action_report = _action_apply_report(agent, ctx, action_options)
    for item in action_report.records:
        records.append(_action_apply_dispatch_record(agent, item))
    return records


# LLM: 复制包含/排除 ID，并传递接管人、文件锁和条数限制；这里只创建选项，不执行动作或写报告。
# 函数用途: 为动作服务封装当前派工范围与是否应用变更的参数。
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


# LLM: 正式路径委托写报告入口，预演路径调用 apply_actions；是否应用动作仍由 options.apply 明确传递。
# 函数用途: 按上下文选择动作处理报告的获取入口并返回原始报告。
def _action_apply_report(agent: Any, ctx: Any, options: ActionApplyOptions):
    if ctx.mutate_state:
        return agent.subagents.actions.write_action_apply_report(ctx.cfg, options=options)
    return agent.subagents.actions.apply_actions(ctx.cfg, options=options)


# LLM: 逐字段保留动作报告的成功、应用标志、前后状态及证据引用；不根据文案重新判定动作结果。
# 函数用途: 将单项动作处理报告转换为派工流水线使用的记录。
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


# LLM: 显式 mutate_state 优先于上下文，控制能力路由是否应用并写报告；父级待裁决也记为可接受结果，缺口不伪装成功。
# 函数用途: 为派工生成能力授权、缺口或父级裁决需求的逐项路由记录。
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
                    action=_capability_route_action(item.status),
                    run_id=item.run_id,
                    dry_run=item.dry_run,
                    applied=not item.dry_run,
                    ok=item.status
                    in {"WOULD_GRANT", "GRANTED", "PARENT_RESOLUTION_REQUIRED"},
                    message=item.message,
                    evidence_paths=[
                        str(agent.subagents.workspace / "subagent_capability_route_report.json")
                    ],
                ),
            )
        )
    return records


# LLM: 空任务列表直接返回空记录；正式路径应用审阅并写报告，预演路径只审阅，逐项保留实际决策与应用结果。
# 函数用途: 将指定子代理的补丁审阅结果转换为本轮派工记录。
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
                    action=_patch_review_action(item.decision),
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


# LLM: 将去除首尾空白的状态文本按固定映射转为动作名；未知状态返回 unknown，不猜测授权结果。
# 函数用途: 为能力路由派工记录提供标准动作标签。
def _capability_route_action(status: object) -> str:
    return _CAPABILITY_ROUTE_ACTIONS.get(str(status or "").strip(), "unknown")


# LLM: 按固定决策映射读取动作名，空值或未知值返回 unknown；只转换标签，不批准或拒绝补丁。
# 函数用途: 为补丁审阅派工记录规范化决策动作标签。
def _patch_review_action(decision: object) -> str:
    return _PATCH_REVIEW_ACTIONS.get(str(decision or "").strip(), "unknown")


# ---------------------------------------------------------------------------
# Pending work state management
# ---------------------------------------------------------------------------


# LLM: 与正式派工共用候选闸；失败预算和能力配置无进展阈值分别传入，保留0关闭，不启动runner。
# 函数用途: 告诉派工循环是否还有子代理 run 需要启动或重跑。
def update_pending_work_state(agent) -> bool:
    from ...runner.dispatch import (
        RunnerCandidatePolicy,
        _dispatch_runner_candidates,
        runner_failure_retry_limit,
    )
    from .tool_helpers import _dispatch_capability_config

    candidates = _dispatch_runner_candidates(
        agent.subagents.list_runs(),
        max_runners=999,
        policy=RunnerCandidatePolicy(
            runner_retry_limit=runner_failure_retry_limit(agent.config),
            runtime_guard_policy=getattr(agent, "runtime_guard_policy", None),
            no_progress_attempt_limit=_dispatch_capability_config(agent).subagent_no_progress_attempt_limit,
        ),
    )
    return len(candidates) > 0


# ---------------------------------------------------------------------------
# Watch mode helpers
# ---------------------------------------------------------------------------


# LLM: 将类型化观察参数原样交给子代理派工服务，不在此解释状态、启动 runner 或额外改写观察结果。
# 函数用途: 为观察模式提供统一 DispatchWatchRecord 构建入口。
def make_dispatch_watch_record(
    agent,
    params: DispatchWatchRecordParams,
) -> DispatchWatchRecord:
    return agent.subagents.dispatch.make_dispatch_watch_record(params=params)
