# LLM: 多轮派工循环只组合单轮派工与统计；重跑次数、并发等数值都读各自唯一配置，不在循环里另设默认。
# 模块用途: 按轮数上限反复执行子代理派工，直到没有待跑工作，并汇总每轮记录。
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_py_agent.agent.capability import CapabilityRouter
    from agent_py_agent.agent.capability.config import CapabilityConfig

    from ..subagents import SubAgent

from ....runtime_errors import runtime_error_report
from .no_progress import DispatchNoProgressTracker
from .params import DispatchExecutionPlan, DispatchParams, DispatchRuntimePolicy


# LLM: 执行计划承载状态写入、启动和并发权限；构造后 max_runners 与计划同步，供循环转换为单轮参数。
# 类用途: 保存多轮派工的停止上限、执行计划和各阶段选项。
@dataclass
class DispatchLoopParams:

    max_consecutive_rounds: int = 20
    execution_plan: DispatchExecutionPlan = field(default_factory=DispatchExecutionPlan)
    planner: bool = False
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None

    # LLM: 仅回写当前参数对象；调用方单独传入的 max_runners 不能覆盖 execution_plan 的并发值。
    # 函数用途: 在循环参数初始化后将并发字段同步为执行计划中的值。
    def __post_init__(self) -> None:
        self.max_runners = int(self.execution_plan.max_runners)


# LLM: 由循环逐轮累加记录数并在退出时补入待跑统计；读取失败与无待跑工作通过独立错误字段区分。
# 类用途: 向派工调用方返回执行轮数、停止原因及最终待跑状态。
@dataclass
class DispatchLoopReport:

    rounds_count: int = 0
    total_records: int = 0
    final_pending_count: int = 0
    final_pending_load_error: dict[str, object] = field(default_factory=dict)
    stopped_by_limit: bool = False
    stopped_by_no_progress: bool = False
    rounds: list[dict] = field(default_factory=list)


_DISPATCH_LOOP_PARAM_KEYS = tuple(field.name for field in fields(DispatchLoopParams))


# LLM: 已给定的参数对象原样返回，类型错误直接拒绝；否则由运行策略和兼容关键字构造参数，不执行派工。
# 函数用途: 统一显式参数对象与旧式关键字调用，供循环入口取得完整参数。
def _coerce_dispatch_loop_params(
    params: DispatchLoopParams | None,
    *,
    policy: DispatchRuntimePolicy | None = None,
    max_consecutive_rounds: int | None = None,
    apply: bool = False,
    execution_plan: DispatchExecutionPlan | None = None,
    planner: bool = False,
    max_runners: int | None = None,
    limit: int | None = None,
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
    runtime_policy = policy or DispatchRuntimePolicy()
    return DispatchLoopParams(
        max_consecutive_rounds=_loop_round_limit(runtime_policy, max_consecutive_rounds),
        execution_plan=_loop_execution_plan(
            runtime_policy,
            apply=apply,
            execution_plan=execution_plan,
            max_runners=max_runners,
        ),
        planner=planner,
        max_runners=_loop_max_runners(runtime_policy, max_runners),
        limit=_loop_limit(runtime_policy, limit),
        reviewer=reviewer,
        note=note,
        runner_instruction=runner_instruction,
        max_cards=max_cards,
        probe=probe,
        take_over_by=take_over_by,
        locked_files=locked_files,
    )


# LLM: 未传值时沿用运行策略；显式值转成非负整数，零由循环解释为不限轮数，转换错误向调用方抛出。
# 函数用途: 为循环参数构造器解析连续派工轮数上限。
def _loop_round_limit(policy: DispatchRuntimePolicy, value: int | None) -> int:
    return policy.max_consecutive_rounds if value is None else max(0, int(value))


# LLM: 只计算候选并发值，不启动工作；未指定时取策略默认值，显式负数收紧为零。
# 函数用途: 为循环执行计划解析最多接纳的 runner 数量。
def _loop_max_runners(policy: DispatchRuntimePolicy, value: int | None) -> int:
    return policy.default_max_runners if value is None else max(0, int(value))


# LLM: 未指定时返回运行策略的记录限额，显式值转为非负整数；不读取代理配置或修改派工状态。
# 函数用途: 为单轮动作处理和审阅阶段解析条数限制。
def _loop_limit(policy: DispatchRuntimePolicy, value: int | None) -> int:
    return policy.default_limit if value is None else max(0, int(value))


# LLM: 优先复用已有计划；兼容 apply 只控制状态写入，新构造的计划明确不启动 runner。
# 函数用途: 将循环兼容参数转换为供单轮派工使用的执行权限计划。
def _loop_execution_plan(
    policy: DispatchRuntimePolicy,
    *,
    apply: bool,
    execution_plan: DispatchExecutionPlan | None,
    max_runners: int | None,
) -> DispatchExecutionPlan:
    return execution_plan or DispatchExecutionPlan.from_parts(
        mutate_state=apply,
        start_runners=False,
        max_runners=_loop_max_runners(policy, max_runners),
    )


# LLM: 解包路由器和能力配置并委托代理正式入口；是否写状态或启动 runner 由传入执行计划决定。
# 函数用途: 为多轮循环执行一次子代理派工并返回该轮报告。
def _run_single_dispatch(agent, routing: tuple[object, object], params: DispatchLoopParams):
    router, capability_config = routing
    return agent.dispatch_subagents(
        router,
        capability_config,
        params=_dispatch_params_from_loop(params),
    )


# LLM: 保留原 execution_plan 和阶段选项，只创建单轮参数容器；循环轮数限制不传给单轮入口。
# 函数用途: 从循环参数提取 dispatch_subagents 所需的单轮参数。
def _dispatch_params_from_loop(params: DispatchLoopParams) -> DispatchParams:
    return DispatchParams(
        execution_plan=params.execution_plan,
        planner=params.planner,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
        runner_instruction=params.runner_instruction,
        max_cards=params.max_cards,
        probe=params.probe,
        take_over_by=params.take_over_by,
        locked_files=params.locked_files,
    )


# LLM: 就地更新累计轮数、记录总数和每轮摘要；成功标志取该轮全部记录的 ok，不改动原单轮报告。
# 函数用途: 将一次派工结果汇总到循环报告，供最终调用方查看。
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


# LLM: 只把 dispatch_loop 的显式关键字装进 DispatchLoopParams；缺省数字由 DispatchRuntimePolicy 的代码默认值补齐，不再读 agent.config。
# 函数用途: 把 dispatch_loop 的 locals() 收成一份循环参数。
def _dispatch_loop_params_from_locals(values: dict) -> DispatchLoopParams:
    selected = {
        key: values[key]
        for key in _DISPATCH_LOOP_PARAM_KEYS
        if key in values and (key != "locked_files" or values[key] is not None)
    }
    selected["apply"] = values.get("apply", False)
    return _coerce_dispatch_loop_params(values["params"], **selected)


# LLM: 按计划反复调用正式派工入口，直到无待跑工作、无进展熔断或达到轮数上限；熔断会清理代理待跑标志。
# 函数用途: 执行有界或不限轮数的子代理派工循环，并返回累计报告与最终候选统计。
def dispatch_loop(
    agent,
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None = None,
    *,
    params: DispatchLoopParams = None,
    max_consecutive_rounds: int | None = None,
    execution_plan: DispatchExecutionPlan | None = None,
    apply: bool = False,
    planner: bool = False,
    max_runners: int | None = None,
    limit: int | None = None,
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
        dispatch_report = _run_single_dispatch(agent, (router, capability_config), params)
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

    report.final_pending_count, report.final_pending_load_error = _final_pending_runner_count(agent)
    return report


# LLM: 与正式派工共用失败预算和能力配置无进展阈值（0关闭）；读账本失败如实报告，
#   不能伪装成“没有待跑任务”。只读，不启动 runner。
# 函数用途: 派工循环结束后统计还有多少子代理 run 等着启动或重跑。
def _final_pending_runner_count(agent) -> tuple[int, dict[str, object]]:
    try:
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
        return len(candidates), {}
    except Exception as exc:
        return 0, {
            "context": "dispatch_loop.final_pending_runner_count",
            "error": runtime_error_report(exc, context="dispatch_loop.final_pending_runner_count"),
        }


# LLM: 仅在无进展停止路径尽力清除代理内存标志；赋值失败记录警告后吞掉异常，不修改任务账本。
# 函数用途: 防止已熔断的派工循环继续以待跑标志触发空转。
def _clear_pending_work(agent) -> None:
    try:
        agent._has_pending_work = False
    except Exception:
        # 容忍:仅是清 pending-work 标志的尽力而为收尾,失败不阻塞分发收口;但不再无声。
        logging.getLogger(__name__).warning("dispatch_loop._clear_pending_work failed", exc_info=True)
        return


__all__ = ["DispatchLoopReport", "DispatchLoopParams", "dispatch_loop"]
