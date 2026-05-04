from __future__ import annotations

"""LLM: thin facade for SimpleAgent parent-dispatch and watch-loop orchestration.

给人看的解释：
这个文件是调度 facade，所有实现都代理到 service 模块。
保持 mixin 签名完全兼容，业务逻辑委托给 dispatch_service、planner_service、runner_gate、acceptance_gate。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchReport, DispatchWatchReport

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..subagent import SubAgentRunnerResult, SubAgentTask


@dataclass
class DispatchParams:
    """Bundle of all dispatch_subagents parameters into a single object."""

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


@dataclass
class WatchParams:
    """Bundle of all dispatch_subagents parameters into a single object."""

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
    interval: float = 30.0
    max_cycles: int = 0
    force_lock: bool = False
    stop_file: str | Path | None = None


@dataclass
class DispatchContext:
    """Internal context bundle for dispatch helpers."""

    cfg: Any
    normalized_workflow_mode: str
    apply: bool
    planner: bool
    runner_instruction: str
    max_runners: int
    limit: int
    reviewer: str
    note: str
    take_over_by: str
    locked_files: list[str] | None
    router: CapabilityRouter
    records: list = field(default_factory=list)


from .dispatch_service import (
    build_workflow_records,
    make_acceptance_records,
    make_action_apply_records,
    make_capability_route_records,
    make_dispatch_watch_record,
    make_due_check_record,
    make_patch_review_records,
    update_pending_work_state,
)
from .failure_introspector import FailureIntrospector
from .planner_service import combine_runner_instruction
from .runner_dispatch import (
    _dispatch_patch_review_run_ids,
    _dispatch_runner_candidates,
    _resolve_runner_concurrency,
    _resolve_runner_start_rate,
    _resolve_runner_timeout_seconds,
    _runner_dispatch_record,
    _runner_max_attempts,
    _runner_retry_reason,
)
from .runner_gate import (
    get_task_timeout,
    handle_runner_failure,
    resolve_runner_config,
    run_concurrent_runners,
    run_single_runner,
)
from .services import notify_completed_tasks
from .services import watch_subagents as _watch_subagents

# ---------------------------------------------------------------------------
# Internal mixin classes – each ≤ 250 lines
# ---------------------------------------------------------------------------


class _DispatchCollectionMixin:
    """Internal: collect and finalize dispatch records (planner, workflow, action, route, patch, acceptance)."""

    # ------------------------------------------------------------------ #
    # _collect_dispatch_records
    # ------------------------------------------------------------------ #

    def _collect_dispatch_records(self, ctx: DispatchContext):
        """Collect records for non-runner dispatch steps."""
        records = []

        if ctx.planner:
            from .subagent_mixin import RunParentPlannerParams

            planner_params = RunParentPlannerParams(
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
            planner_record = self.run_parent_planner(planner_params)
            records.append(
                self.subagents.make_dispatch_record(
                    step="parent_planner",
                    action=planner_record.decision.lower(),
                    dry_run=not ctx.apply,
                    applied=False,
                    ok=planner_record.ok,
                    message=planner_record.message,
                    evidence_paths=planner_record.evidence_paths,
                )
            )

        if ctx.normalized_workflow_mode in {"plan", "auto"}:
            workflow_records = build_workflow_records(
                self, self.subagents.list_runs(), ctx.normalized_workflow_mode, ctx.limit, ctx.apply
            )
            records.extend(workflow_records)

        records.append(make_due_check_record(self, ctx.cfg, ctx.apply))

        action_records = make_action_apply_records(
            self, ctx.cfg, ctx.apply, ctx.take_over_by, ctx.locked_files, ctx.limit
        )
        records.extend(action_records)

        route_records = make_capability_route_records(
            self, ctx.router, ctx.cfg, ctx.apply, ctx.limit
        )
        records.extend(route_records)

        return records

    # ------------------------------------------------------------------ #
    # _execute_runner_jobs
    # ------------------------------------------------------------------ #

    def _execute_runner_jobs(
        self,
        ctx: DispatchContext,
        execute_runners: bool,
        max_cards: int,
        probe: bool,
        existing_records: list,
    ) -> list:
        """Execute runner jobs and return updated records."""
        records = list(existing_records)
        effective_runner_instruction = ctx.runner_instruction
        runner_max_attempts = _runner_max_attempts(self.config.runner_failure_policy)
        runner_candidates = _dispatch_runner_candidates(
            self.subagents.list_runs(), ctx.max_runners, runner_max_attempts=runner_max_attempts
        )
        pending_runner_jobs = []

        for task in runner_candidates:
            before = self.subagents.load(task.id)
            retry_reason = _runner_retry_reason(before, runner_max_attempts)
            action_name = "retry_runner" if retry_reason else "execute_runner"
            if not ctx.apply:
                records.append(
                    self.subagents.make_dispatch_record(
                        step="runner",
                        action=action_name,
                        run_id=task.id,
                        dry_run=True,
                        applied=False,
                        ok=True,
                        message=(
                            f"dry-run: 将重试 runner（{retry_reason}）。"
                            if retry_reason
                            else "dry-run: apply 时会生成执行上下文；带 --execute-runners 时会调用模型。"
                        ),
                        before_status=before.status,
                        after_status=before.status,
                        before_verification_status=before.verification_status,
                        after_verification_status=before.verification_status,
                        evidence_paths=[before.task_dir],
                    )
                )
                continue
            pending_runner_jobs.append((task.id, before, retry_reason))

        if pending_runner_jobs:
            runner_timeout_seconds, runner_concurrency, runner_start_rate = resolve_runner_config(
                self.config, len(pending_runner_jobs)
            )
            if runner_start_rate and runner_start_rate < len(pending_runner_jobs):
                pending_runner_jobs = pending_runner_jobs[:runner_start_rate]

            if pending_runner_jobs and runner_concurrency > 1 and execute_runners:
                completed = run_concurrent_runners(
                    self,
                    pending_runner_jobs,
                    runner_concurrency,
                    runner_timeout_seconds,
                    effective_runner_instruction,
                    execute_runners,
                    max_cards,
                    probe,
                )
                for run_id, before, retry_reason in pending_runner_jobs:
                    result, after = completed[run_id]
                    records.append(
                        _runner_dispatch_record(
                            self,
                            run_id=run_id,
                            before=before,
                            after=after,
                            result=result,
                            retry_reason=retry_reason,
                            execute_runners=execute_runners,
                        )
                    )
                    if not result.ok and execute_runners:
                        effective_runner_instruction = handle_runner_failure(
                            self, run_id, before, result, effective_runner_instruction
                        )
            else:
                for run_id, before, retry_reason in pending_runner_jobs:
                    task_timeout = get_task_timeout(before, runner_timeout_seconds, self.config)
                    result = run_single_runner(
                        self,
                        run_id,
                        task_timeout,
                        effective_runner_instruction,
                        execute_runners,
                        max_cards,
                        probe,
                        retry_reason,
                    )
                    after = self.subagents.load(run_id)
                    records.append(
                        _runner_dispatch_record(
                            self,
                            run_id=run_id,
                            before=before,
                            after=after,
                            result=result,
                            retry_reason=retry_reason,
                            execute_runners=execute_runners,
                        )
                    )
                    if not result.ok and execute_runners:
                        effective_runner_instruction = handle_runner_failure(
                            self, run_id, before, result, effective_runner_instruction
                        )

        ctx.runner_instruction = effective_runner_instruction
        return records

    # ------------------------------------------------------------------ #
    # _finalize_dispatch
    # ------------------------------------------------------------------ #

    def _finalize_dispatch(self, cfg, apply, reviewer, note, limit, existing_records):
        """Add patch review and acceptance records."""
        records = list(existing_records)
        patch_run_ids = _dispatch_patch_review_run_ids(self.subagents.list_runs())
        patch_records = make_patch_review_records(self, patch_run_ids, apply, reviewer, note, limit)
        records.extend(patch_records)

        acceptance_records = make_acceptance_records(self, apply, reviewer, note, limit)
        records.extend(acceptance_records)
        return records


class _DispatchReportMixin:
    """Internal: build and write dispatch reports, notify completed tasks."""

    def _build_and_write_report(self, records, apply):
        """Build and write dispatch report."""
        report = self.subagents.build_dispatch_report(records, dry_run=not apply)
        report = self.subagents.write_dispatch_report(report, append_log=apply)
        if apply:
            notify_completed_tasks(self, records)
        self._has_pending_work = update_pending_work_state(self)
        return report

    def _notify_completed_tasks(self, records: list) -> None:
        """Thin wrapper for backward compatibility — delegates to services.notify_completed_tasks."""
        notify_completed_tasks(self, records)


class _DispatchFailureMixin:
    """Internal: failure introspection after runner errors."""

    def _handle_failure_introspection(self, run_id, task_before, runner_result):
        """失败后调用 LLM 自省，分析原因并给出调参建议。"""
        try:
            from .failure_analyzer import SubAgentFailureAnalyzer

            task = self.subagents.load(run_id)
            analyzer = SubAgentFailureAnalyzer()
            failure_analysis = analyzer.analyze(task, runner_result)

            from .failure_introspector import FailureIntrospector

            introspector = FailureIntrospector(agent=self)
            introspection = introspector.introspect(task, runner_result, failure_analysis)

            introspection_data = {
                "analysis_reason": introspection.analysis_reason,
                "root_cause": introspection.root_cause,
                "suggested_params": introspection.suggested_params,
                "should_retry": introspection.should_retry,
                "should_split": introspection.should_split,
                "confidence": introspection.confidence,
            }
            task.attributes["failure_introspection_data"] = introspection_data

            if introspection.suggested_params:
                self._apply_introspection_params(task, introspection.suggested_params)

            self.subagents.save(task)

        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(f"Failure introspection failed: {exc}")

    def _apply_introspection_params(self, task, params):
        """根据 LLM 自省结果调整任务参数。"""
        applied = []
        if "new_timeout_seconds" in params:
            timeout = float(params["new_timeout_seconds"])
            task.attributes["dynamic_timeout_seconds"] = timeout
            applied.append(f"timeout={timeout}s")
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

            logging.getLogger(__name__).info(
                f"Applied LLM introspection params to {task.id}: {applied}"
            )

        return task

    def _update_pending_work_state(self) -> None:
        """更新待处理工作状态。"""
        self._has_pending_work = update_pending_work_state(self)


class _DispatchFacadeMixin:
    """Internal: state properties and watch_subagents facade only."""

    # 闭环检测相关属性
    _has_pending_work: bool = False
    _consecutive_dispatch_rounds: int = 0

    @property
    def has_pending_work(self) -> bool:
        """公开的待处理工作状态属性。"""
        return self._has_pending_work

    def _increment_dispatch_rounds(self) -> None:
        """递增连续 dispatch 轮数。"""
        self._consecutive_dispatch_rounds += 1

    def _reset_dispatch_rounds(self) -> None:
        """重置连续 dispatch 轮数。"""
        self._consecutive_dispatch_rounds = 0

    # ------------------------------------------------------------------ #
    # watch_subagents
    # ------------------------------------------------------------------ #

    def watch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        params: WatchParams = None,
        **kwargs,
    ) -> DispatchWatchReport:
        """以 watch 模式持续执行父代理调度。

        Thin facade that delegates to watch_service.
        """
        # Backward compatibility: accept kwargs and merge into WatchParams
        if params is None:
            params = WatchParams()
        elif isinstance(params, WatchParams):
            pass
        else:
            raise TypeError("watch_subagents() requires params: WatchParams keyword argument")

        # Merge kwargs for backward compatibility
        for key in [
            "apply",
            "execute_runners",
            "planner",
            "workflow_mode",
            "max_runners",
            "limit",
            "reviewer",
            "note",
            "runner_instruction",
            "max_cards",
            "probe",
            "take_over_by",
            "locked_files",
            "interval",
            "max_cycles",
            "force_lock",
            "stop_file",
        ]:
            if key in kwargs:
                setattr(params, key, kwargs[key])

        from .services.watch_service import WatchSubagentsParams as _WSP

        wsp = _WSP(
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
            interval=params.interval,
            max_cycles=params.max_cycles,
            force_lock=params.force_lock,
            stop_file=params.stop_file,
        )
        return _watch_subagents(
            self,
            router=router,
            capability_config=capability_config,
            params=wsp,
        )


class SimpleAgentDispatchMixin(
    _DispatchFacadeMixin,
    _DispatchCollectionMixin,
    _DispatchReportMixin,
    _DispatchFailureMixin,
):
    """LLM: mixin for audited parent dispatch and recurring watch mode.

    给人看的解释：
    用户说"推进一下子代理"或 daemon 定时巡检，都会走这里。
    它负责串阶段，不把底层文件操作和规则判断都塞在自己身上。
    """

    def dispatch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
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
        """执行一轮父代理调度。

        dry-run 只汇总会做什么；apply 会依次执行低风险动作、能力路由、
        runner、patch 审核和父代理验收。真实模型调用还需要额外打开
        `execute_runners`，避免普通 apply 意外消耗 API。
        """
        cfg = capability_config or CapabilityConfig()
        normalized_workflow_mode = str(workflow_mode or "off").strip().lower()
        effective_runner_instruction = runner_instruction
        effective_max_runners = max_runners

        ctx = DispatchContext(
            cfg=cfg,
            normalized_workflow_mode=normalized_workflow_mode,
            apply=apply,
            planner=planner,
            runner_instruction=effective_runner_instruction,
            max_runners=effective_max_runners,
            limit=limit,
            reviewer=reviewer,
            note=note,
            take_over_by=take_over_by,
            locked_files=locked_files,
            router=router,
        )
        records = self._collect_dispatch_records(ctx)

        effective_max_runners = max_runners
        if planner:
            planner_record = records[0] if records else None
            if planner_record and planner_record.step == "parent_planner":
                effective_runner_instruction = combine_runner_instruction(
                    runner_instruction,
                    getattr(planner_record, "message", "").split("instruction:")[-1].strip()
                    if "instruction:" in planner_record.message
                    else "",
                )
                if (
                    hasattr(planner_record, "suggested_max_runners")
                    and planner_record.suggested_max_runners > 0
                ):
                    effective_max_runners = min(
                        max_runners, planner_record.suggested_max_runners
                    )

        records = self._execute_runner_jobs(
            ctx, execute_runners, max_cards, probe, records
        )
        ctx.records = records
        ctx.runner_instruction = effective_runner_instruction
        ctx.max_runners = effective_max_runners

        records = self._finalize_dispatch(
            cfg, apply, reviewer, note, limit, records
        )

        return self._build_and_write_report(records, apply)
