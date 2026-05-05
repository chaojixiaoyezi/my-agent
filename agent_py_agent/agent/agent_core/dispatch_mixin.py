from __future__ import annotations

"""LLM: thin facade for SimpleAgent parent-dispatch and watch-loop orchestration.

给人看的解释：
这个文件是调度 facade，所有实现都代理到 service 模块。
保持 mixin 签名完全兼容，业务逻辑委托给 dispatch_service、planner_service、runner_gate、acceptance_gate。
"""

from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchReport
from .dispatch_facade import _DispatchFacadeMixin, _DispatchFailureMixin
from .dispatch_params import DispatchContext, DispatchParams, RunnerBatchContext, WatchParams

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
    _dispatch_runner_candidates,
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

# ---------------------------------------------------------------------------
# Internal mixin classes – each ≤ 250 lines
# ---------------------------------------------------------------------------


class _DispatchCollectionMixin:
    """Internal: collect and finalize dispatch records."""

    def _collect_dispatch_records(self, ctx: DispatchContext):
        """Collect records for non-runner dispatch steps."""
        records = []

        if ctx.planner:
            from .subagent_mixin import RunParentPlannerParams

            planner_params = RunParentPlannerParams(
                router=ctx.router, capability_config=ctx.cfg, apply=ctx.apply,
                execute_runners=False, max_runners=ctx.max_runners, limit=ctx.limit,
                reviewer=ctx.reviewer, note=ctx.note, runner_instruction=ctx.runner_instruction,
            )
            planner_record = self.run_parent_planner(planner_params)
            records.append(
                self.subagents.make_dispatch_record(
                    step="parent_planner", action=planner_record.decision.lower(),
                    dry_run=not ctx.apply, applied=False, ok=planner_record.ok,
                    message=planner_record.message, evidence_paths=planner_record.evidence_paths,
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

    def _collect_runner_candidates(
        self, ctx: DispatchContext, runner_max_attempts: int, runner_candidates: list
    ) -> tuple[list, list]:
        """Collect runner candidates and pending jobs."""
        pending_runner_jobs, dry_records = [], []
        for task in runner_candidates:
            before = self.subagents.load(task.id)
            retry_reason = _runner_retry_reason(before, runner_max_attempts)
            action_name = "retry_runner" if retry_reason else "execute_runner"
            if not ctx.apply:
                dry_records.append(
                    self.subagents.make_dispatch_record(
                        step="runner", action=action_name, run_id=task.id,
                        dry_run=True, applied=False, ok=True,
                        message=(
                            f"dry-run: 将重试 runner（{retry_reason}）。"
                            if retry_reason
                            else "dry-run: apply 时会生成执行上下文；带 --execute-runners 时会调用模型。"
                        ),
                        before_status=before.status, after_status=before.status,
                        before_verification_status=before.verification_status,
                        after_verification_status=before.verification_status,
                        evidence_paths=[before.task_dir],
                    )
                )
                continue
            pending_runner_jobs.append((task.id, before, retry_reason))
        return dry_records, pending_runner_jobs

    def _execute_runner_jobs(
        self, ctx: DispatchContext, execute_runners: bool,
        max_cards: int, probe: bool, existing_records: list,
    ) -> list:
        """Execute runner jobs and return updated records."""
        records = list(existing_records)
        effective_runner_instruction = ctx.runner_instruction
        runner_max_attempts = _runner_max_attempts(self.config.runner_failure_policy)
        runner_candidates = _dispatch_runner_candidates(
            self.subagents.list_runs(), ctx.max_runners, runner_max_attempts=runner_max_attempts
        )

        dry_records, pending_runner_jobs = self._collect_runner_candidates(
            ctx, runner_max_attempts, runner_candidates
        )
        records.extend(dry_records)

        if pending_runner_jobs:
            runner_timeout_seconds, runner_concurrency, runner_start_rate = resolve_runner_config(
                self.config, len(pending_runner_jobs)
            )
            if runner_start_rate and runner_start_rate < len(pending_runner_jobs):
                pending_runner_jobs = pending_runner_jobs[:runner_start_rate]

            if pending_runner_jobs and runner_concurrency > 1 and execute_runners:
                batch_ctx = RunnerBatchContext(
                    pending_runner_jobs=pending_runner_jobs,
                    runner_concurrency=runner_concurrency,
                    runner_timeout_seconds=runner_timeout_seconds,
                    effective_runner_instruction=effective_runner_instruction,
                    execute_runners=execute_runners,
                    max_cards=max_cards,
                    probe=probe,
                    records=records,
                )
                records, effective_runner_instruction = self._run_concurrent_batch(batch_ctx)
            else:
                batch_ctx = RunnerBatchContext(
                    pending_runner_jobs=pending_runner_jobs,
                    runner_concurrency=0,
                    runner_timeout_seconds=runner_timeout_seconds,
                    effective_runner_instruction=effective_runner_instruction,
                    execute_runners=execute_runners,
                    max_cards=max_cards,
                    probe=probe,
                    records=records,
                )
                records, effective_runner_instruction = self._run_sequential_batch(batch_ctx)
            ctx.runner_instruction = effective_runner_instruction
        return records

    def _run_concurrent_batch(self, ctx: RunnerBatchContext) -> tuple[list, str]:
        """Run runner jobs concurrently. Returns (records, updated_instruction)."""
        completed = run_concurrent_runners(
            self, ctx.pending_runner_jobs, ctx.runner_concurrency, ctx.runner_timeout_seconds,
            ctx.effective_runner_instruction, ctx.execute_runners, ctx.max_cards, ctx.probe,
        )
        for run_id, before, retry_reason in ctx.pending_runner_jobs:
            result, after = completed[run_id]
            ctx.records.append(
                _runner_dispatch_record(
                    self, run_id=run_id, before=before, after=after,
                    result=result, retry_reason=retry_reason, execute_runners=ctx.execute_runners,
                )
            )
            if not result.ok and ctx.execute_runners:
                ctx.effective_runner_instruction = handle_runner_failure(
                    self, run_id, before, result, ctx.effective_runner_instruction
                )
        return ctx.records, ctx.effective_runner_instruction

    def _run_sequential_batch(self, ctx: RunnerBatchContext) -> tuple[list, str]:
        """Run runner jobs sequentially. Returns (records, updated_instruction)."""
        for run_id, before, retry_reason in ctx.pending_runner_jobs:
            task_timeout = get_task_timeout(before, ctx.runner_timeout_seconds, self.config)
            result = run_single_runner(
                self, run_id, task_timeout, ctx.effective_runner_instruction,
                ctx.execute_runners, ctx.max_cards, ctx.probe, retry_reason,
            )
            after = self.subagents.load(run_id)
            ctx.records.append(
                _runner_dispatch_record(
                    self, run_id=run_id, before=before, after=after,
                    result=result, retry_reason=retry_reason, execute_runners=ctx.execute_runners,
                )
            )
            if not result.ok and ctx.execute_runners:
                ctx.effective_runner_instruction = handle_runner_failure(
                    self, run_id, before, result, ctx.effective_runner_instruction
                )
        return ctx.records, ctx.effective_runner_instruction

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
        params: DispatchParams | None = None,
        **kwargs,
    ) -> DispatchReport:
        """执行一轮父代理调度。

        dry-run 只汇总会做什么；apply 会依次执行低风险动作、能力路由、
        runner、patch 审核和父代理验收。真实模型调用还需要额外打开
        `execute_runners`，避免普通 apply 意外消耗 API。
        """
        if params is None:
            params = DispatchParams()
        elif not isinstance(params, DispatchParams):
            raise TypeError("dispatch_subagents() requires params: DispatchParams keyword argument")
        for key in [
            "apply", "execute_runners", "planner", "workflow_mode",
            "max_runners", "limit", "reviewer", "note", "runner_instruction",
            "max_cards", "probe", "take_over_by", "locked_files",
        ]:
            if key in kwargs:
                setattr(params, key, kwargs[key])

        cfg = capability_config or CapabilityConfig()
        normalized_workflow_mode = str(params.workflow_mode or "off").strip().lower()
        effective_runner_instruction = params.runner_instruction
        effective_max_runners = params.max_runners

        ctx = DispatchContext(
            cfg=cfg, normalized_workflow_mode=normalized_workflow_mode,
            apply=params.apply, planner=params.planner,
            runner_instruction=effective_runner_instruction,
            max_runners=effective_max_runners, limit=params.limit,
            reviewer=params.reviewer, note=params.note,
            take_over_by=params.take_over_by, locked_files=params.locked_files,
            router=router,
        )
        records = self._collect_dispatch_records(ctx)

        if params.planner:
            planner_record = records[0] if records else None
            if planner_record and planner_record.step == "parent_planner":
                effective_runner_instruction = combine_runner_instruction(
                    params.runner_instruction,
                    getattr(planner_record, "message", "").split("instruction:")[-1].strip()
                    if "instruction:" in planner_record.message else "",
                )
                if (
                    hasattr(planner_record, "suggested_max_runners")
                    and planner_record.suggested_max_runners > 0
                ):
                    effective_max_runners = min(params.max_runners, planner_record.suggested_max_runners)

        records = self._execute_runner_jobs(
            ctx, params.execute_runners, params.max_cards, params.probe, records
        )
        ctx.records = records
        ctx.runner_instruction = effective_runner_instruction
        ctx.max_runners = effective_max_runners

        records = self._finalize_dispatch(
            cfg, params.apply, params.reviewer, params.note, params.limit, records
        )
        return self._build_and_write_report(records, params.apply)