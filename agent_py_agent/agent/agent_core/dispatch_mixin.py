# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""thin facade for SimpleAgent parent-dispatch and watch-loop orchestration.

缁欎汉鐪嬬殑瑙ｉ噴锛?
杩欎釜鏂囦欢鏄皟搴?facade锛屾墍鏈夊疄鐜伴兘浠ｇ悊鍒?service 妯″潡銆?
淇濇寔 mixin 绛惧悕瀹屽叏鍏煎锛屼笟鍔￠€昏緫濮旀墭缁?dispatch_service銆乸lanner_service銆乺unner_gate銆乤cceptance_gate銆?
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchReport
from .dispatch_facade import _DispatchFacadeMixin, _DispatchFailureMixin
from .dispatch_mixin_helpers import (
    DispatchRunnerStageRequest,
    RunnerJobExecutionParams,
    parent_planner_dispatch_record,
    run_dispatch_runner_stage,
)
from .dispatch_params import (
    DispatchContext,
    DispatchParams,
    RunnerBatchContext,
    merge_dispatch_params,
)
from .dispatch_record_params import (
    AcceptanceRecordParams,
    ActionApplyRecordParams,
    CapabilityRouteRecordParams,
    PatchReviewRecordParams,
    WorkflowRecordParams,
)
from .dispatch_runner_batches import execute_runner_jobs

if TYPE_CHECKING:
    pass

from .dispatch_service import (
    build_workflow_records,
    make_acceptance_records,
    make_action_apply_records,
    make_capability_route_records,
    make_due_check_record,
    make_patch_review_records,
    update_pending_work_state,
)
from .failure_introspector import FailureIntrospector
from .planner_service import combine_runner_instruction
from .runner_dispatch import (
    _dispatch_patch_review_run_ids,
)
from .services import notify_completed_tasks

# ---------------------------------------------------------------------------
# Internal mixin classes 鈥?each 鈮?250 lines
# ---------------------------------------------------------------------------


# LLM: DispatchFinalizeParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存调度finalize参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class DispatchFinalizeParams:
    apply: bool
    reviewer: str
    note: str
    limit: int
    existing_records: list


# LLM: _DispatchCollectionBase 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装调度collection基础相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _DispatchCollectionBase:

    # LLM: _collect_dispatch_records 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 读取或查询记录需要的状态，返回调用方可继续处理的快照；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _collect_dispatch_records(self, ctx: DispatchContext):
        records = []

        if ctx.planner:
            records.append(parent_planner_dispatch_record(self, ctx))

        if ctx.normalized_workflow_mode in {"plan", "auto"}:
            workflow_records = build_workflow_records(
                WorkflowRecordParams(
                    self,
                    self.subagents.list_runs(),
                    ctx.normalized_workflow_mode,
                    ctx.limit,
                    ctx.apply,
                )
            )
            records.extend(workflow_records)

        records.append(make_due_check_record(self, ctx.cfg, ctx.apply))
        action_records = make_action_apply_records(
            ActionApplyRecordParams(
                self,
                ctx.cfg,
                ctx.apply,
                ctx.take_over_by,
                ctx.locked_files,
                ctx.limit,
            )
        )
        records.extend(action_records)
        route_records = make_capability_route_records(
            CapabilityRouteRecordParams(self, ctx.router, ctx.cfg, ctx.apply, ctx.limit)
        )
        records.extend(route_records)
        return records

    # LLM: _execute_runner_jobs 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进执行器jobs的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _execute_runner_jobs(
        self, params: RunnerJobExecutionParams,
    ) -> list:
        batch_ctx = RunnerBatchContext(
            pending_runner_jobs=[],
            runner_concurrency=0,
            runner_timeout_seconds=0,
            effective_runner_instruction=params.ctx.runner_instruction,
            execute_runners=params.execute_runners,
            max_cards=params.max_cards,
            probe=params.probe,
            records=list(params.existing_records),
        )
        records = execute_runner_jobs(self, params.ctx, batch_ctx)
        params.ctx.runner_instruction = batch_ctx.effective_runner_instruction
        return records

    # LLM: _finalize_dispatch 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理finalize调度相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _finalize_dispatch(self, params: DispatchFinalizeParams):
        records = list(params.existing_records)
        patch_run_ids = _dispatch_patch_review_run_ids(self.subagents.list_runs())
        patch_records = make_patch_review_records(
            PatchReviewRecordParams(
                self, patch_run_ids, params.apply, params.reviewer, params.note, params.limit
            )
        )
        records.extend(patch_records)
        acceptance_records = make_acceptance_records(
            AcceptanceRecordParams(self, params.apply, params.reviewer, params.note, params.limit)
        )
        records.extend(acceptance_records)
        return records


# LLM: _DispatchReportMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分调度报告混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _DispatchReportMixin:

    # LLM: _build_and_write_report 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建write报告所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _build_and_write_report(self, records, apply):
        report = self.subagents.build_dispatch_report(records, dry_run=not apply)
        report = self.subagents.write_dispatch_report(report, append_log=apply)
        if apply:
            notify_completed_tasks(self, records)
        self._has_pending_work = update_pending_work_state(self)
        return report

    # LLM: _notify_completed_tasks 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理notifycompletedtasks相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _notify_completed_tasks(self, records: list) -> None:
        notify_completed_tasks(self, records)


# LLM: SimpleAgentDispatchMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分simpleagent调度混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class SimpleAgentDispatchMixin(
    _DispatchFacadeMixin,
    _DispatchCollectionBase,
    _DispatchReportMixin,
    _DispatchFailureMixin,
):

    # LLM: dispatch_subagents 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进子代理的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def dispatch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        params: DispatchParams | None = None,
        apply: bool = False,
        execute_runners: bool = False,
        planner: bool = False,
        workflow_mode: str = "off",
        max_runners: int = 1,
        limit: int = 20,
        reviewer: str = "parent-dispatch",
        note: str = "",
        runner_instruction: str = "",
        max_cards: int = 0,
        probe: bool = True,
        take_over_by: str = "",
        locked_files: list[str] | None = None,
    ) -> DispatchReport:
        params = _dispatch_params_from_call(
            params=params,
            apply=apply,
            execute_runners=execute_runners,
            planner=planner,
            workflow_mode=workflow_mode,
            max_runners=max_runners,
            limit=limit,
            reviewer=reviewer,
            note=note,
            runner_instruction=runner_instruction,
            max_cards=max_cards,
            probe=probe,
            take_over_by=take_over_by,
            locked_files=locked_files,
        )
        ctx = _dispatch_context_from_params(router, capability_config, params)
        records = self._collect_dispatch_records(ctx)

        if params.planner:
            ctx.runner_instruction, ctx.max_runners = _planner_dispatch_overrides(params, records)

        records = run_dispatch_runner_stage(request=DispatchRunnerStageRequest(self, ctx, params, records))
        ctx.records = records
        records = self._finalize_dispatch(_dispatch_finalize_params(params, records))
        return self._build_and_write_report(records, params.apply)


# LLM: _dispatch_params_from_call 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进来自参数call的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _dispatch_params_from_call(
    *,
    params: DispatchParams | None,
    apply: bool,
    execute_runners: bool,
    planner: bool,
    workflow_mode: str,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
    runner_instruction: str,
    max_cards: int,
    probe: bool,
    take_over_by: str,
    locked_files: list[str] | None,
) -> DispatchParams:
    params = params or DispatchParams(
        apply=apply,
        execute_runners=execute_runners,
        planner=planner,
        workflow_mode=workflow_mode,
        max_runners=max_runners,
        limit=limit,
        reviewer=reviewer,
        note=note,
        runner_instruction=runner_instruction,
        max_cards=max_cards,
        probe=probe,
        take_over_by=take_over_by,
        locked_files=locked_files,
    )
    return merge_dispatch_params(params)


# LLM: _dispatch_context_from_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进来自上下文参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _dispatch_context_from_params(
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None,
    params: DispatchParams,
) -> DispatchContext:
    return DispatchContext(
        cfg=capability_config or CapabilityConfig(),
        normalized_workflow_mode=str(params.workflow_mode or "off").strip().lower(),
        apply=params.apply,
        planner=params.planner,
        runner_instruction=params.runner_instruction,
        max_runners=params.max_runners,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
        take_over_by=params.take_over_by,
        locked_files=params.locked_files,
        router=router,
    )


# LLM: _dispatch_finalize_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进finalize参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _dispatch_finalize_params(params: DispatchParams, records: list) -> DispatchFinalizeParams:
    return DispatchFinalizeParams(
        apply=params.apply,
        reviewer=params.reviewer,
        note=params.note,
        limit=params.limit,
        existing_records=records,
    )


# LLM: _planner_dispatch_overrides 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理规划器调度overrides相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _planner_dispatch_overrides(params: DispatchParams, records):
    planner_record = records[0] if records else None
    if not planner_record or planner_record.step != "parent_planner":
        return params.runner_instruction, params.max_runners
    instruction = combine_runner_instruction(
        params.runner_instruction,
        getattr(planner_record, "message", "").split("instruction:")[-1].strip()
        if "instruction:" in planner_record.message else "",
    )
    max_runners = params.max_runners
    if hasattr(planner_record, "suggested_max_runners") and planner_record.suggested_max_runners > 0:
        max_runners = min(params.max_runners, planner_record.suggested_max_runners)
    return instruction, max_runners
