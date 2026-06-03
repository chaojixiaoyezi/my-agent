

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ....capabilities import CapabilityRouter
from ....capability_config import CapabilityConfig
from ....subagent import DispatchWatchReport
from ...services import watch_subagents as _watch_subagents
from .params import DispatchExecutionPlan, WatchParams, merge_watch_params
from .service import update_pending_work_state

if TYPE_CHECKING:
    pass


class _DispatchFacadeMixin:

    _has_pending_work: bool = False
    _consecutive_dispatch_rounds: int = 0

    @property
    def has_pending_work(self) -> bool:
        return self._has_pending_work

    def _increment_dispatch_rounds(self) -> None:
        self._consecutive_dispatch_rounds += 1

    def _reset_dispatch_rounds(self) -> None:
        self._consecutive_dispatch_rounds = 0

    def watch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        params: WatchParams = None,
        execution_plan: DispatchExecutionPlan | None = None,
        apply: bool = False,
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
        params = params or _watch_params_from_compat_args(locals())
        params = merge_watch_params(params)
        return _watch_subagents(self, router=router, capability_config=capability_config, params=params)


class _DispatchFailureMixin:

    def _handle_failure_introspection(self, run_id, task_before, runner_result):
        try:
            from ...failure_analyzer import SubAgentFailureAnalyzer

            task = self.subagents.load(run_id)
            analyzer = SubAgentFailureAnalyzer()
            failure_analysis = analyzer.analyze(task, runner_result)

            from ...failure_introspector import FailureIntrospector

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

    def _update_pending_work_state(self) -> None:
        self._has_pending_work = update_pending_work_state(self)


def _watch_params_from_compat_args(values: Mapping[str, Any]) -> WatchParams:
    """Keep legacy facade kwargs at the boundary before the unified params object."""

    max_runners = int(values.get("max_runners") or 1)
    execution_plan = values.get("execution_plan")
    return WatchParams(
        execution_plan=execution_plan
        or DispatchExecutionPlan.from_parts(
            mutate_state=bool(values.get("apply")),
            start_runners=False,
            max_runners=max_runners,
        ),
        planner=bool(values.get("planner")),
        workflow_mode=str(values.get("workflow_mode") or "off"),
        limit=int(values.get("limit") or 0),
        reviewer=str(values.get("reviewer") or ""),
        note=str(values.get("note") or ""),
        runner_instruction=str(values.get("runner_instruction") or ""),
        max_cards=int(values.get("max_cards") or 0),
        probe=bool(values.get("probe")),
        take_over_by=str(values.get("take_over_by") or ""),
        locked_files=values.get("locked_files"),
        interval=float(values.get("interval") or 0.0),
        max_cycles=int(values.get("max_cycles") or 0),
        advance=bool(values.get("advance")),
        force_lock=bool(values.get("force_lock")),
        stop_file=values.get("stop_file"),
    )
