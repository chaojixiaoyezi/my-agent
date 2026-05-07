
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
            force_lock=force_lock,
            stop_file=stop_file,
        )
        params = merge_watch_params(params)
        return _watch_subagents(self, router=router, capability_config=capability_config, params=params)


class _DispatchFailureMixin:

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
