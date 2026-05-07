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
from .dispatch_facade import _DispatchFacadeMixin, _DispatchFailureMixin
from .dispatch_mixin_helpers import (
    DispatchRunnerStageRequest,
    RunnerJobExecutionParams,
    parent_planner_dispatch_record,
    run_dispatch_runner_stage,
)
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
            records.append(parent_planner_dispatch_record(self, ctx))

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
        params = _dispatch_params_from_call(
            params=params,
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
        ctx = _dispatch_context_from_params(router, capability_config, params)
        records = self._collect_dispatch_records(ctx)

        if params.planner:
            ctx.runner_instruction, ctx.max_runners = _planner_dispatch_overrides(params, records)

        records = run_dispatch_runner_stage(request=DispatchRunnerStageRequest(self, ctx, params, records))
        ctx.records = records
        records = self._finalize_dispatch(_dispatch_finalize_params(params, records))
        return self._build_and_write_report(records, params.apply)


def _dispatch_params_from_call(
    *,
    params: DispatchParams | None,
    apply: bool,
    execute_runners: bool,
    planner: bool,
    workflow_mode: str,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
    runner_instruction: str,
    max_cards: int,
    probe: bool,
    take_over_by: str,
    locked_files: list[str] | None,
) -> DispatchParams:
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
    return merge_dispatch_params(params)


def _dispatch_context_from_params(
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None,
    params: DispatchParams,
) -> DispatchContext:
    return DispatchContext(
        cfg=capability_config or CapabilityConfig(),
        normalized_workflow_mode=str(params.workflow_mode or "off").strip().lower(),
        apply=params.apply,
        planner=params.planner,
        runner_instruction=params.runner_instruction,
        max_runners=params.max_runners,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
        take_over_by=params.take_over_by,
        locked_files=params.locked_files,
        router=router,
    )


def _dispatch_finalize_params(params: DispatchParams, records: list) -> DispatchFinalizeParams:
    return DispatchFinalizeParams(
        apply=params.apply,
        reviewer=params.reviewer,
        note=params.note,
        limit=params.limit,
        existing_records=records,
    )


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
