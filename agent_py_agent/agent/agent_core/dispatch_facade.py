
from __future__ import annotations

from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchWatchReport
from .dispatch_params import WatchParams
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
        **kwargs,
    ) -> DispatchWatchReport:
        if params is None:
            params = WatchParams()
        elif not isinstance(params, WatchParams):
            raise TypeError("watch_subagents() requires params: WatchParams keyword argument")

        for key in [
            "apply", "execute_runners", "planner", "workflow_mode",
            "max_runners", "limit", "reviewer", "note", "runner_instruction",
            "max_cards", "probe", "take_over_by", "locked_files",
            "interval", "max_cycles", "force_lock", "stop_file",
        ]:
            if key in kwargs:
                setattr(params, key, kwargs[key])

        from .services.watch_service import WatchSubagentsParams as _WSP

        wsp = _WSP(
            apply=params.apply, execute_runners=params.execute_runners,
            planner=params.planner, workflow_mode=params.workflow_mode,
            max_runners=params.max_runners, limit=params.limit,
            reviewer=params.reviewer, note=params.note,
            runner_instruction=params.runner_instruction, max_cards=params.max_cards,
            probe=params.probe, take_over_by=params.take_over_by,
            locked_files=params.locked_files, interval=params.interval,
            max_cycles=params.max_cycles, force_lock=params.force_lock,
            stop_file=params.stop_file,
        )
        return _watch_subagents(self, router=router, capability_config=capability_config, params=wsp)


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