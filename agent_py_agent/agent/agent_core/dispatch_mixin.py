from __future__ import annotations

"""LLM: thin facade for SimpleAgent parent-dispatch and watch-loop orchestration.

缁欎汉鐪嬬殑瑙ｉ噴锛?
杩欎釜鏂囦欢鏄皟搴?facade锛屾墍鏈夊疄鐜伴兘浠ｇ悊鍒?service 妯″潡銆?
淇濇寔 mixin 绛惧悕瀹屽叏鍏煎锛屼笟鍔￠€昏緫濮旀墭缁?dispatch_service銆乸lanner_service銆乺unner_gate銆乤cceptance_gate銆?
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchReport
from ..subagents.services.dispatch_params import DispatchRecordParams
from .dispatch_facade import _DispatchFacadeMixin, _DispatchFailureMixin
from .dispatch_params import (
    DispatchContext,
    DispatchParams,
    RunnerBatchContext,
    merge_dispatch_params,
)
from .dispatch_record_params import (
    AcceptanceRecordParams,
    ActionApplyRecordParams,
    CapabilityRouteRecordParams,
    PatchReviewRecordParams,
    WorkflowRecordParams,
)
from .dispatch_runner_batches import execute_runner_jobs

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
)
from .services import notify_completed_tasks

# ---------------------------------------------------------------------------
# Internal mixin classes 鈥?each 鈮?250 lines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerJobExecutionParams:
    ctx: DispatchContext
    execute_runners: bool
    max_cards: int
    probe: bool
    existing_records: list


@dataclass(frozen=True)
class DispatchFinalizeParams:
    apply: bool
    reviewer: str
    note: str
    limit: int
    existing_records: list


class _DispatchCollectionBase:

    def _collect_dispatch_records(self, ctx: DispatchContext):
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
                    params=DispatchRecordParams(
                        step="parent_planner", action=planner_record.decision.lower(),
                        dry_run=not ctx.apply, applied=False, ok=planner_record.ok,
                        message=planner_record.message, evidence_paths=planner_record.evidence_paths,
                    ),
                )
            )

        if ctx.normalized_workflow_mode in {"plan", "auto"}:
            workflow_records = build_workflow_records(
                WorkflowRecordParams(
                    self,
                    self.subagents.list_runs(),
                    ctx.normalized_workflow_mode,
                    ctx.limit,
                    ctx.apply,
                )
            )
            records.extend(workflow_records)

        records.append(make_due_check_record(self, ctx.cfg, ctx.apply))
        action_records = make_action_apply_records(
            ActionApplyRecordParams(
                self,
                ctx.cfg,
                ctx.apply,
                ctx.take_over_by,
                ctx.locked_files,
                ctx.limit,
            )
        )
        records.extend(action_records)
        route_records = make_capability_route_records(
            CapabilityRouteRecordParams(self, ctx.router, ctx.cfg, ctx.apply, ctx.limit)
        )
        records.extend(route_records)
        return records

    def _execute_runner_jobs(
        self, params: RunnerJobExecutionParams,
    ) -> list:
        batch_ctx = RunnerBatchContext(
            pending_runner_jobs=[],
            runner_concurrency=0,
            runner_timeout_seconds=0,
            effective_runner_instruction=params.ctx.runner_instruction,
            execute_runners=params.execute_runners,
            max_cards=params.max_cards,
            probe=params.probe,
            records=list(params.existing_records),
        )
        records = execute_runner_jobs(self, params.ctx, batch_ctx)
        params.ctx.runner_instruction = batch_ctx.effective_runner_instruction
        return records

    def _finalize_dispatch(self, params: DispatchFinalizeParams):
        records = list(params.existing_records)
        patch_run_ids = _dispatch_patch_review_run_ids(self.subagents.list_runs())
        patch_records = make_patch_review_records(
            PatchReviewRecordParams(
                self, patch_run_ids, params.apply, params.reviewer, params.note, params.limit
            )
        )
        records.extend(patch_records)
        acceptance_records = make_acceptance_records(
            AcceptanceRecordParams(self, params.apply, params.reviewer, params.note, params.limit)
        )
        records.extend(acceptance_records)
        return records


class _DispatchReportMixin:

    def _build_and_write_report(self, records, apply):
        report = self.subagents.build_dispatch_report(records, dry_run=not apply)
        report = self.subagents.write_dispatch_report(report, append_log=apply)
        if apply:
            notify_completed_tasks(self, records)
        self._has_pending_work = update_pending_work_state(self)
        return report

    def _notify_completed_tasks(self, records: list) -> None:
        notify_completed_tasks(self, records)


class SimpleAgentDispatchMixin(
    _DispatchFacadeMixin,
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
        params = params or DispatchParams(
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
        params = merge_dispatch_params(params)

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
            effective_runner_instruction, effective_max_runners = _planner_dispatch_overrides(
                params, records
            )

        records = self._execute_runner_jobs(
            RunnerJobExecutionParams(
                ctx=ctx,
                execute_runners=params.execute_runners,
                max_cards=params.max_cards,
                probe=params.probe,
                existing_records=records,
            )
        )
        ctx.records = records
        ctx.runner_instruction = effective_runner_instruction
        ctx.max_runners = effective_max_runners

        records = self._finalize_dispatch(
            DispatchFinalizeParams(
                apply=params.apply,
                reviewer=params.reviewer,
                note=params.note,
                limit=params.limit,
                existing_records=records,
            )
        )
        return self._build_and_write_report(records, params.apply)

def _planner_dispatch_overrides(params: DispatchParams, records):
    planner_record = records[0] if records else None
    if not planner_record or planner_record.step != "parent_planner":
        return params.runner_instruction, params.max_runners
    instruction = combine_runner_instruction(
        params.runner_instruction,
        getattr(planner_record, "message", "").split("instruction:")[-1].strip()
        if "instruction:" in planner_record.message else "",
    )
    max_runners = params.max_runners
    if hasattr(planner_record, "suggested_max_runners") and planner_record.suggested_max_runners > 0:
        max_runners = min(params.max_runners, planner_record.suggested_max_runners)
    return instruction, max_runners
