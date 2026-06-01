# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""helper records for the dispatch mixin facade."""

from dataclasses import dataclass

from ..subagents.services.dispatch_params import DispatchRecordParams
from .dispatch_params import DispatchContext, DispatchParams


# LLM: RunnerJobExecutionParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器jobexecution参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerJobExecutionParams:
    ctx: DispatchContext
    execute_runners: bool
    max_cards: int
    probe: bool
    existing_records: list


# LLM: DispatchRunnerStageRequest 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存调度执行器stage请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class DispatchRunnerStageRequest:
    # LLM: 调度执行器阶段输入集中成包，便于控制体积并扩展调度字段。
    agent: object
    ctx: DispatchContext
    params: DispatchParams
    records: list


# LLM: parent_planner_dispatch_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理父级规划器调度记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def parent_planner_dispatch_record(agent, ctx: DispatchContext):
    from .subagent_mixin import RunParentPlannerParams

    planner_record = agent.run_parent_planner(
        RunParentPlannerParams(
            router=ctx.router,
            capability_config=ctx.cfg,
            apply=ctx.apply,
            execute_runners=False,
            max_runners=ctx.max_runners,
            limit=ctx.limit,
            reviewer=ctx.reviewer,
            note=ctx.note,
            runner_instruction=ctx.runner_instruction,
        )
    )
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="parent_planner",
            action=planner_record.decision.lower(),
            dry_run=not ctx.apply,
            applied=False,
            ok=planner_record.ok,
            message=planner_record.message,
            evidence_paths=planner_record.evidence_paths,
        ),
    )


# LLM: run_dispatch_runner_stage 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器stage的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_dispatch_runner_stage(
    agent=None,
    *,
    request: DispatchRunnerStageRequest | None = None,
    ctx: DispatchContext | None = None,
    params: DispatchParams | None = None,
    records: list | None = None,
) -> list:
    # LLM: 执行器运行参数保持独立参数包，调度门面只负责传递。
    request = request or DispatchRunnerStageRequest(agent, ctx, params, records or [])
    return request.agent._execute_runner_jobs(
        RunnerJobExecutionParams(
            ctx=request.ctx,
            execute_runners=request.params.execution_plan.start_runners,
            max_cards=request.params.max_cards,
            probe=request.params.probe,
            existing_records=request.records,
        )
    )
