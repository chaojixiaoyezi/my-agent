
from __future__ import annotations

"""SimpleAgent parent-dispatch and watch-loop orchestration."""

from collections.abc import Mapping
from typing import Any

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ....subagents import DispatchReport, DispatchWatchReport
from ...failure_introspector import FailureIntrospector
from ...planner_service import combine_runner_instruction
from ...runner.dispatch import (
    _dispatch_patch_review_run_ids,
)
from ...services import notify_completed_tasks
from ...services import watch_subagents as _watch_subagents
from .capability_followup import (
    run_post_runner_capability_followup,
)
from .collection_records import collect_dispatch_records
from .params import (
    DispatchContext,
    DispatchExecutionPlan,
    DispatchParams,
    RunnerBatchContext,
    WatchParams,
    merge_dispatch_params,
    merge_watch_params,
)
from .runner_batches import execute_runner_jobs, run_dispatch_runner_stage
from .runner_selection import scoped_runner_tasks
from .service import (
    make_patch_review_records,
    update_pending_work_state,
)


class _DispatchWatchMixin:
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
        recovery_mode: str = "",
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
        watch_params = params or _watch_params_from_args(locals())
        watch_params = merge_watch_params(watch_params)
        return _watch_subagents(self, router=router, capability_config=capability_config, params=watch_params)


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


class _DispatchCollectionBase:

    def _collect_dispatch_records(self, ctx: DispatchContext):
        return collect_dispatch_records(self, ctx)

    def _execute_runner_jobs(self, *, ctx: DispatchContext, max_cards: int, probe: bool, records: list) -> list:
        batch_ctx = RunnerBatchContext(
            pending_runner_jobs=[],
            runner_concurrency=0,
            runner_timeout_seconds=0,
            effective_runner_instruction=ctx.runner_instruction,
            max_cards=max_cards,
            probe=probe,
            records=list(records),
            execution_plan=ctx.execution_plan,
        )
        records = execute_runner_jobs(self, ctx, batch_ctx)
        ctx.runner_instruction = batch_ctx.effective_runner_instruction
        return records

    def _finalize_dispatch(self, params: DispatchParams, records: list):
        records = list(records)
        patch_run_ids = _dispatch_patch_review_run_ids(_finalize_scoped_tasks(self, params))
        patch_records = make_patch_review_records(
            self,
            patch_run_ids,
            params,
        )
        records.extend(patch_records)
        return records


class _DispatchReportMixin:

    def _build_and_write_report(self, records, execution_plan: DispatchExecutionPlan):
        mutate_state = bool(execution_plan.mutate_state)
        report = self.subagents.dispatch.build_dispatch_report(records, dry_run=not mutate_state)
        report = self.subagents.dispatch.write_dispatch_report(report, append_log=mutate_state)
        if mutate_state:
            notify_completed_tasks(self, records)
        self._has_pending_work = update_pending_work_state(self)
        return report

    def _notify_completed_tasks(self, records: list) -> None:
        notify_completed_tasks(self, records)


class SimpleAgentDispatchMixin(
    _DispatchWatchMixin,
    _DispatchCollectionBase,
    _DispatchReportMixin,
    _DispatchFailureMixin,
):

    def dispatch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        params: DispatchParams | None = None,
        execution_plan: DispatchExecutionPlan | None = None,
        apply: bool = False,
        start_runners: bool = False,
        planner: bool = False,
        workflow_mode: str = "off",
        max_runners: int = 1,
        limit: int = 20,
        reviewer: str = "parent-dispatch",
        note: str = "",
        runner_instruction: str = "",
        recovery_mode: str = "",
        max_cards: int = 0,
        probe: bool = True,
        take_over_by: str = "",
        locked_files: list[str] | None = None,
    ) -> DispatchReport:
        params = _dispatch_params_from_call(
            params=params,
            execution_plan=execution_plan,
            apply=apply,
            start_runners=start_runners,
            planner=planner,
            workflow_mode=workflow_mode,
            max_runners=max_runners,
            limit=limit,
            reviewer=reviewer,
            note=note,
            runner_instruction=runner_instruction,
            recovery_mode=recovery_mode,
            max_cards=max_cards,
            probe=probe,
            take_over_by=take_over_by,
            locked_files=locked_files,
        )
        return _run_dispatch_from_params(self, (router, capability_config), params)


def _run_dispatch_from_params(
    agent: SimpleAgentDispatchMixin,
    routing: tuple[CapabilityRouter, CapabilityConfig | None],
    params: DispatchParams,
) -> DispatchReport:
    router, capability_config = routing
    ctx = _dispatch_context_from_params(router, capability_config, params)
    records = agent._collect_dispatch_records(ctx)

    if params.planner:
        ctx.runner_instruction, ctx.max_runners = _planner_dispatch_overrides(params, records)

    records = run_dispatch_runner_stage(agent, ctx=ctx, params=params, records=records)
    records = run_post_runner_capability_followup(agent, (ctx, params, records))
    ctx.records = records
    records = agent._finalize_dispatch(params, records)
    return agent._build_and_write_report(records, params.execution_plan)


def _dispatch_params_from_call(
    *,
    params: DispatchParams | None,
    execution_plan: DispatchExecutionPlan | None,
    apply: bool,
    start_runners: bool,
    planner: bool,
    workflow_mode: str,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
    runner_instruction: str,
    recovery_mode: str,
    max_cards: int,
    probe: bool,
    take_over_by: str,
    locked_files: list[str] | None,
) -> DispatchParams:
    plan = execution_plan or DispatchExecutionPlan.from_parts(
        mutate_state=apply,
        start_runners=start_runners,
        max_runners=max_runners,
    )
    params = params or DispatchParams(
        execution_plan=plan,
        planner=planner,
        workflow_mode=workflow_mode,
        limit=limit,
        reviewer=reviewer,
        note=note,
        runner_instruction=runner_instruction,
        recovery_mode=recovery_mode,
        max_cards=max_cards,
        probe=probe,
        take_over_by=take_over_by,
        locked_files=locked_files,
    )
    return merge_dispatch_params(params)


def _watch_params_from_args(values: Mapping[str, Any]) -> WatchParams:
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
        recovery_mode=str(values.get("recovery_mode") or ""),
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


def _dispatch_context_from_params(
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None,
    params: DispatchParams,
) -> DispatchContext:
    plan = params.execution_plan
    return DispatchContext(
        cfg=capability_config or CapabilityConfig(),
        normalized_workflow_mode=str(params.workflow_mode or "off").strip().lower(),
        planner=params.planner,
        runner_instruction=params.runner_instruction,
        recovery_mode=params.recovery_mode,
        max_runners=plan.max_runners,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
        take_over_by=params.take_over_by,
        locked_files=params.locked_files,
        parent_run_id=params.parent_run_id,
        root_id=params.root_id,
        include_run_ids=params.include_run_ids,
        exclude_run_ids=params.exclude_run_ids,
        background_launch_id=params.background_launch_id,
        router=router,
        execution_plan=plan,
    )


def _finalize_scoped_tasks(agent, params: DispatchParams) -> list:
    ctx = DispatchContext(
        cfg=CapabilityConfig(),
        normalized_workflow_mode="off",
        planner=False,
        runner_instruction="",
        recovery_mode=params.recovery_mode,
        max_runners=params.execution_plan.max_runners,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
        take_over_by="",
        locked_files=None,
        parent_run_id=params.parent_run_id,
        root_id=params.root_id,
        include_run_ids=params.include_run_ids,
        exclude_run_ids=params.exclude_run_ids,
        background_launch_id="",
        router=CapabilityRouter(),
        execution_plan=params.execution_plan,
    )
    return scoped_runner_tasks(agent.subagents.list_runs(), ctx)


def _planner_dispatch_overrides(params: DispatchParams, records):
    planner_record = records[0] if records else None
    if not planner_record or planner_record.step != "parent_planner":
        return params.runner_instruction, params.execution_plan.max_runners
    instruction = combine_runner_instruction(
        params.runner_instruction,
        str(getattr(planner_record, "runner_instruction", "") or "").strip(),
    )
    max_runners = params.execution_plan.max_runners
    if hasattr(planner_record, "suggested_max_runners") and planner_record.suggested_max_runners > 0:
        max_runners = min(params.execution_plan.max_runners, planner_record.suggested_max_runners)
    return instruction, max_runners
