# LLM: Dispatch collection helpers keep the SimpleAgent mixin as a thin facade.
# 模块用途: 汇总 planner、workflow、due-check、action apply 和 capability route 的 dispatch records。

from __future__ import annotations

from .dispatch_mixin_helpers import parent_planner_dispatch_record
from .dispatch_record_params import (
    ActionApplyRecordParams,
    CapabilityRouteRecordParams,
    WorkflowRecordParams,
)
from .dispatch_service import (
    DueCheckRecordParams,
    build_workflow_records,
    make_action_apply_records,
    make_capability_route_records,
    make_due_check_record,
    make_leadership_recovery_plan_record,
)


# LLM: collect_dispatch_records assembles one dispatch cycle from small record builder helpers.
# 函数用途: 按顺序收集 planner、workflow、due-check、leadership、action 和 capability route 记录。
def collect_dispatch_records(agent, ctx):
    records = []
    records.extend(_planner_and_workflow_records(agent, ctx))
    records.extend(_due_and_leadership_records(agent, ctx))
    records.extend(_action_apply_records(agent, ctx))
    records.extend(_capability_route_records(agent, ctx))
    return records


# LLM: _planner_and_workflow_records handles optional planner and workflow dry-run steps.
# 函数用途: 根据 dispatch 模式生成 planner 记录和 workflow plan/auto 记录。
def _planner_and_workflow_records(agent, ctx) -> list:
    records = []
    if ctx.planner:
        records.append(parent_planner_dispatch_record(agent, ctx))
    if ctx.normalized_workflow_mode in {"plan", "auto"}:
        records.extend(
            build_workflow_records(
                WorkflowRecordParams(
                    agent,
                    agent.subagents.list_runs(),
                    ctx.normalized_workflow_mode,
                    ctx.limit,
                    ctx.apply,
                    override_task_off=True,
                )
            )
        )
    return records


# LLM: _due_and_leadership_records adds due-check and optional coordinator recovery plan records.
# 函数用途: 生成 due-check 记录，并在发现 stale coordinator 时追加 leadership recovery plan。
def _due_and_leadership_records(agent, ctx) -> list:
    records = [
        make_due_check_record(
            DueCheckRecordParams(
                agent,
                ctx.cfg,
                ctx.apply,
                root_id=ctx.root_id,
                include_run_ids=ctx.include_run_ids,
                exclude_run_ids=ctx.exclude_run_ids,
            )
        )
    ]
    leadership_record = make_leadership_recovery_plan_record(agent, ctx.cfg)
    if leadership_record is not None:
        records.append(leadership_record)
    return records


# LLM: _action_apply_records maps dispatch context into scoped action apply records.
# 函数用途: 让 action apply 继承 root/include/exclude 范围，避免误处理其他任务树。
def _action_apply_records(agent, ctx) -> list:
    return make_action_apply_records(
        ActionApplyRecordParams(
            agent,
            ctx.cfg,
            ctx.apply,
            ctx.take_over_by,
            ctx.locked_files,
            ctx.limit,
            ctx.root_id,
            ctx.include_run_ids,
            ctx.exclude_run_ids,
        )
    )


# LLM: _capability_route_records appends capability grant/deny audit records to dispatch output.
# 函数用途: 对 OPEN capability request 做 dry-run 或 apply 路由，并写入 dispatch records。
def _capability_route_records(agent, ctx) -> list:
    return make_capability_route_records(
        CapabilityRouteRecordParams(agent, ctx.router, ctx.cfg, ctx.apply, ctx.limit)
    )
