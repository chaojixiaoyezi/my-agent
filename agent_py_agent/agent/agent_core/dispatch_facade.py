# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchWatchReport
from .dispatch_params import WatchParams, merge_watch_params
from .dispatch_service import update_pending_work_state
from .services import watch_subagents as _watch_subagents

if TYPE_CHECKING:
    pass


# LLM: _DispatchFacadeMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分调度门面混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _DispatchFacadeMixin:

    _has_pending_work: bool = False
    _consecutive_dispatch_rounds: int = 0

    # LLM: has_pending_work 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 判断pendingwork条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    @property
    def has_pending_work(self) -> bool:
        return self._has_pending_work

    # LLM: _increment_dispatch_rounds 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理increment调度轮数相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _increment_dispatch_rounds(self) -> None:
        self._consecutive_dispatch_rounds += 1

    # LLM: _reset_dispatch_rounds 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 更新轮数对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _reset_dispatch_rounds(self) -> None:
        self._consecutive_dispatch_rounds = 0

    # LLM: watch_subagents 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: watch 默认巡检子代理状态；显式 advance=True 时才串接调度、runner 和验收。
    def watch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        params: WatchParams = None,
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
        interval: float = 30.0,
        max_cycles: int = 0,
        advance: bool = False,
        force_lock: bool = False,
        stop_file=None,
    ) -> DispatchWatchReport:
        params = params or WatchParams(
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
            interval=interval,
            max_cycles=max_cycles,
            advance=advance,
            force_lock=force_lock,
            stop_file=stop_file,
        )
        params = merge_watch_params(params)
        return _watch_subagents(self, router=router, capability_config=capability_config, params=params)


# LLM: _DispatchFailureMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分调度失败混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _DispatchFailureMixin:

    # LLM: _handle_failure_introspection 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进失败introspection的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _handle_failure_introspection(self, run_id, task_before, runner_result):
        try:
            from .failure_analyzer import SubAgentFailureAnalyzer

            task = self.subagents.load(run_id)
            analyzer = SubAgentFailureAnalyzer()
            failure_analysis = analyzer.analyze(task, runner_result)

            from .failure_introspector import FailureIntrospector

            introspector = FailureIntrospector(agent=self)
            introspection = introspector.introspect(task, runner_result, failure_analysis)

            task.attributes["failure_introspection_data"] = {
                "analysis_reason": introspection.analysis_reason,
                "root_cause": introspection.root_cause,
                "suggested_params": introspection.suggested_params,
                "should_retry": introspection.should_retry,
                "should_split": introspection.should_split,
                "confidence": introspection.confidence,
            }
            if introspection.suggested_params:
                self._apply_introspection_params(task, introspection.suggested_params)
            self.subagents.save(task)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(f"Failure introspection failed: {exc}")

    # LLM: _apply_introspection_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 更新introspection参数对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新运行循环、工具调用、调度记录和最终响应，需避免破坏既有状态机约定。
    def _apply_introspection_params(self, task, params):
        applied = []
        if "new_timeout_seconds" in params:
            task.attributes["dynamic_timeout_seconds"] = float(params["new_timeout_seconds"])
            applied.append(f"timeout={params['new_timeout_seconds']}s")
        if "max_tool_rounds" in params:
            task.attributes["max_tool_rounds"] = int(params["max_tool_rounds"])
            applied.append(f"max_tool_rounds={params['max_tool_rounds']}")
        if "split_goal" in params:
            split_goal = str(params["split_goal"])
            if split_goal and task.goal:
                task.goal = f"{task.goal} | {split_goal}"
                applied.append(f"goal_adjustment={split_goal}")
        if applied:
            import logging
            logging.getLogger(__name__).info(f"Applied LLM introspection params to {task.id}: {applied}")
        return task

    # LLM: _update_pending_work_state 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 更新pendingwork状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新运行循环、工具调用、调度记录和最终响应，需避免破坏既有状态机约定。
    def _update_pending_work_state(self) -> None:
        self._has_pending_work = update_pending_work_state(self)
