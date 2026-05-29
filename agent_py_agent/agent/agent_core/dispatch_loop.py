# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..capabilities import CapabilityRouter
    from ..capability_config import CapabilityConfig
    from ..subagent import SubAgent

from .dispatch_no_progress import DispatchNoProgressTracker
from .dispatch_params import DispatchParams


# LLM: DispatchLoopParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存调度循环参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchLoopParams:

    max_consecutive_rounds: int = 20
    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None


# LLM: DispatchLoopReport 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存调度循环报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchLoopReport:

    rounds_count: int = 0
    total_records: int = 0
    final_pending_count: int = 0
    stopped_by_limit: bool = False
    stopped_by_no_progress: bool = False
    rounds: list[dict] = field(default_factory=list)


# LLM: SingleDispatchRequest 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存单个调度请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class SingleDispatchRequest:
    agent: object
    router: object
    capability_config: object
    params: DispatchLoopParams


_DISPATCH_LOOP_PARAM_KEYS = tuple(field.name for field in fields(DispatchLoopParams))


# LLM: _coerce_dispatch_loop_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 解析并归一化循环参数的输入形态，让下游只处理稳定结构；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _coerce_dispatch_loop_params(
    params: DispatchLoopParams | None,
    *,
    max_consecutive_rounds: int = 20,
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
) -> DispatchLoopParams:
    if params is not None and not isinstance(params, DispatchLoopParams):
        raise TypeError("dispatch_loop() requires params: DispatchLoopParams keyword argument")

    if params is not None:
        return params
    return DispatchLoopParams(
        max_consecutive_rounds=max_consecutive_rounds,
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


# LLM: _run_single_dispatch 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进单个调度的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_single_dispatch(request: SingleDispatchRequest):
    params = request.params
    return request.agent.dispatch_subagents(
        request.router,
        request.capability_config,
        params=_dispatch_params_from_loop(params),
    )


# LLM: _dispatch_params_from_loop 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进来自参数循环的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _dispatch_params_from_loop(params: DispatchLoopParams) -> DispatchParams:
    return DispatchParams(
        apply=params.apply,
        execute_runners=params.execute_runners,
        planner=params.planner,
        workflow_mode=params.workflow_mode,
        max_runners=params.max_runners,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
        runner_instruction=params.runner_instruction,
        max_cards=params.max_cards,
        probe=params.probe,
        take_over_by=params.take_over_by,
        locked_files=params.locked_files,
    )


# LLM: _append_dispatch_round 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入round的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _append_dispatch_round(report: DispatchLoopReport, dispatch_report, round_num: int) -> None:
    report.rounds_count = round_num
    report.total_records += len(dispatch_report.records)
    report.rounds.append(
        {
            "round": round_num,
            "record_count": len(dispatch_report.records),
            "ok": all(item.ok for item in dispatch_report.records),
        }
    )


# LLM: _dispatch_loop_params_from_locals 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进来自循环参数locals的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _dispatch_loop_params_from_locals(values: dict) -> DispatchLoopParams:
    return _coerce_dispatch_loop_params(
        values["params"],
        **{
            key: values[key]
            for key in _DISPATCH_LOOP_PARAM_KEYS
            if key != "locked_files" or values[key] is not None
        },
    )


# LLM: dispatch_loop 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进循环的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def dispatch_loop(
    agent,
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None = None,
    *,
    params: DispatchLoopParams = None,
    max_consecutive_rounds: int = 20,
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
) -> DispatchLoopReport:
    params = _dispatch_loop_params_from_locals(locals())
    report = DispatchLoopReport()
    max_rounds = params.max_consecutive_rounds
    no_progress_tracker = DispatchNoProgressTracker()
    round_num = 0

    while max_rounds == 0 or round_num < max_rounds:
        round_num += 1
        dispatch_report = _run_single_dispatch(
            SingleDispatchRequest(agent, router, capability_config, params)
        )
        _append_dispatch_round(report, dispatch_report, round_num)
        if not agent.has_pending_work:
            break
        if no_progress_tracker.should_stop(dispatch_report):
            report.stopped_by_no_progress = True
            _clear_pending_work(agent)
            break
        if max_rounds > 0 and round_num >= max_rounds:
            report.stopped_by_limit = True
            break

    report.final_pending_count = _final_pending_runner_count(agent)
    return report


# LLM: _final_pending_runner_count 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理finalpending执行器数量相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _final_pending_runner_count(agent) -> int:
    try:
        from .runner_candidate_policy import RunnerCandidatePolicy
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
            policy=RunnerCandidatePolicy(
                runner_max_attempts=runner_max_attempts,
                same_run_redispatch_limit=same_run_limit,
            ),
        )
        return len(candidates)
    except Exception:
        return 0


# LLM: _clear_pending_work lets a no-progress fuse settle the outer run loop.
# 函数用途: 当调度只剩重复审计记录时，清掉 agent 待处理标记，让上层循环自然收口而不是继续空转。
def _clear_pending_work(agent) -> None:
    try:
        agent._has_pending_work = False
    except Exception:
        return


__all__ = ["DispatchLoopReport", "DispatchLoopParams", "dispatch_loop"]
