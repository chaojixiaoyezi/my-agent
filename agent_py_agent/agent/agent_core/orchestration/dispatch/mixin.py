
from __future__ import annotations

import json

"""SimpleAgent parent-dispatch and watch-loop orchestration."""

from collections.abc import Mapping
from typing import Any

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ....subagents import DispatchReport, DispatchWatchReport
from ....subagents.models import TaskStatus
from ...failure_analysis_service import FailureIntrospector
from ...planner_service import combine_runner_instruction
from ...runner.dispatch import (
    _dispatch_patch_review_run_ids,
)
from ...services.watch_service import watch_subagents as _watch_subagents
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


# LLM: runner 失败自省的应用层。链路：runner/gate.py handle_runner_failure →
#   _handle_failure_introspection →（规则分析 SubAgentFailureAnalyzer + 自省
#   FailureIntrospector）→ _apply_introspection_params（调参落 attributes）+
#   _apply_introspection_split（自动拆分，受 capability_config 的
#   subagent_failure_auto_split_enabled / subagent_failure_split_max_depth 控制）。
#   契约：自省是软增强，任何一步失败不得阻断 runner 主链路；但失败必须结构化留痕
#   （task.attributes["failure_introspection_error"]），不允许只吞日志。
#   suggested_params 的生产端在 failure_analysis_service._suggest_params_from_analysis，
#   改动消费 key 时必须保持两端对齐。改动时同步检查
#   tests/test_real_class_integration.py、tests/test_dispatch_mixin.py。
# 类用途: 子代理 runner 失败后的"自省→调参→自动拆分"落地；失败原因分析逻辑在
#   failure_analysis_service，这里只负责把建议真正写回任务、拆出子任务并落盘。
class _DispatchFailureMixin:
    # LLM: 唯一入口；load 失败只能日志，load 之后的失败写 failure_introspection_error
    #   并尽力落盘。不抛异常（软增强契约）。副作用：保存任务与拆分出的子任务。
    # 函数用途: runner 失败后做一次自省，把调参/拆分建议真正应用到任务上。
    def _handle_failure_introspection(self, run_id, task_before, runner_result):
        try:
            task = self.subagents.load(run_id)
        except Exception as exc:
            _log_introspection_failure(run_id, exc, stage="load")
            return
        try:
            self._run_failure_introspection(task, runner_result)
            self.subagents.save(task)
        except Exception as exc:
            _log_introspection_failure(run_id, exc, stage="apply")
            self._record_introspection_error(task, exc)

    # LLM: 自省主体：分析→自省→记录结构化结果→调参→自动拆分。拆分必须在
    #   introspection 数据写入 attributes 之后调用（拆分结果会补记进同一条记录）。
    # 函数用途: 跑完整自省链路并把结果写进 task.attributes（不落盘，由调用方 save）。
    def _run_failure_introspection(self, task, runner_result) -> None:
        from ...failure_analysis_service import SubAgentFailureAnalyzer

        analyzer = SubAgentFailureAnalyzer()
        failure_analysis = analyzer.analyze(task, runner_result)

        introspector = FailureIntrospector()
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
            self._record_introspection_lesson(task, introspection)
        self._apply_introspection_split(task, introspection)

    # LLM: 失败自省只提交 model_inferred lesson Candidate；统一审核/Promotion 之前不得成为可召回正文。
    # 函数用途: 把结构化失败与调参建议投递到 owner 唯一候选账本，供后续人工审核和重复验证。
    def _record_introspection_lesson(self, task, introspection) -> None:
        candidate_adapter = getattr(
            getattr(self, "subagents", None),
            "memory_candidates",
            None,
        )
        record = getattr(candidate_adapter, "record_introspection_lesson", None)
        if not callable(record):
            return
        failure_type = str(getattr(task, "failure_type", "") or "")
        try:
            record(
                task,
                suggested_params=dict(introspection.suggested_params or {}),
                failure_type=failure_type,
                attempts=max(0, int(getattr(task, "runner_attempts", 0) or 0)),
                confidence=float(getattr(introspection, "confidence", 0.0) or 0.0),
            )
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "introspection lesson candidate write failed",
                exc_info=True,
            )

    # LLM: 消费 key 必须与生产端对齐：new_timeout_seconds（runner 超时，读取方
    #   get_task_timeout）、max_tool_rounds（工具轮上限，读取方 _effective_max_tool_rounds；
    #   规则自省暂不产出，保留给 LLM 自省/人工注入）。split_suggestions 不在这里消费，
    #   走 _apply_introspection_split。已删除的 split_goal 字符串拼接分支不得回加
    #   （拆分的唯一权威是 split_task 子任务，不允许影子拆分路径）。
    # 函数用途: 把自省建议的数值参数写进任务 attributes（不落盘，由调用方 save）。
    def _apply_introspection_params(self, task, params):
        applied = []
        if "new_timeout_seconds" in params:
            task.attributes["dynamic_timeout_seconds"] = float(params["new_timeout_seconds"])
            applied.append(f"timeout={params['new_timeout_seconds']}s")
        if "max_tool_rounds" in params:
            task.attributes["max_tool_rounds"] = int(params["max_tool_rounds"])
            applied.append(f"max_tool_rounds={params['max_tool_rounds']}")
        if applied:
            import logging

            logging.getLogger(__name__).info(f"Applied failure introspection params to {task.id}: {applied}")
        return task

    # LLM: split_suggestions 的唯一消费方。决策全部基于结构化字段：should_split、
    #   split_suggestions、task.depth、capability_config 开关；跳过原因结构化记录在
    #   failure_introspection_data.split_skipped_reason。拆分动作复用
    #   failure_analysis_service.split_task（原任务转 TAKEN_OVER + split_into）。
    #   子任务先落盘、原任务后落盘（崩溃时宁可残留可重拆的失败任务，不可出现
    #   "原任务已接管但子任务丢失"）。副作用：保存子任务。
    # 函数用途: 自省判定该拆分时，把失败任务拆成子任务重新派工；返回拆出的子任务。
    def _apply_introspection_split(self, task, introspection) -> list:
        suggestions = [
            str(item) for item in (introspection.suggested_params or {}).get("split_suggestions") or [] if str(item).strip()
        ]
        record = task.attributes.get("failure_introspection_data")
        record = record if isinstance(record, dict) else {}
        if not introspection.should_split or not suggestions:
            return []
        enabled, max_depth = _failure_auto_split_settings(self)
        if not enabled:
            record["split_applied"] = False
            record["split_skipped_reason"] = "auto_split_disabled"
            return []
        if max_depth and int(getattr(task, "depth", 0) or 0) >= max_depth:
            record["split_applied"] = False
            record["split_skipped_reason"] = f"depth_limit:{max_depth}"
            return []
        from ...failure_analysis_service import split_task

        subtasks = split_task(task, suggestions)
        for subtask in subtasks:
            subtask.status = TaskStatus.PLANNING.value
            self.subagents.save(subtask)
        record["split_applied"] = True
        record["split_into"] = [subtask.id for subtask in subtasks]
        return subtasks

    # LLM: 自省 apply 段失败的结构化留痕；留痕本身再失败时只能降级日志（不抛）。
    # 函数用途: 把自省失败原因写进任务 attributes 并尽力落盘，供后续排查。
    def _record_introspection_error(self, task, exc) -> None:
        try:
            from ....runtime_errors import runtime_error_report

            task.attributes["failure_introspection_error"] = runtime_error_report(
                exc, context="dispatch.failure_introspection"
            )
            self.subagents.save(task)
        except Exception as save_exc:
            _log_introspection_failure(getattr(task, "id", ""), save_exc, stage="record_error")

    def _update_pending_work_state(self) -> None:
        self._has_pending_work = update_pending_work_state(self)


# LLM: 自省失败日志的统一出口；stage 取 load/apply/record_error，方便日志检索。
# 函数用途: 打一条带阶段标记的自省失败 warning 日志。
def _log_introspection_failure(run_id: str, exc: BaseException, *, stage: str) -> None:
    import logging

    logging.getLogger(__name__).warning(
        f"Failure introspection {stage} failed for {run_id}: {exc.__class__.__name__}: {exc}"
    )


# LLM: 失败自省自动拆分的配置读取；配置来源唯一权威是
#   capability.runtime_config_reload.capability_config_for_agent（快照→缓存加载）。
#   返回 (enabled, max_depth)；max_depth=0 表示不限制（项目统一约定）。
# 函数用途: 读"自动拆分开关 + 拆分深度上限"两个 capability 配置项。
def _failure_auto_split_settings(agent) -> tuple[bool, int]:
    from ....capability.runtime_config_reload import capability_config_for_agent

    config = capability_config_for_agent(agent)
    enabled = bool(getattr(config, "subagent_failure_auto_split_enabled", False))
    try:
        max_depth = int(getattr(config, "subagent_failure_split_max_depth", 2) or 0)
    except (TypeError, ValueError):
        max_depth = 2
    return enabled, max_depth


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
        self._has_pending_work = update_pending_work_state(self)
        return report


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
    router = getattr(agent, "capability_router", None)
    if not isinstance(router, CapabilityRouter):
        raise RuntimeError("agent capability router is unavailable")
    ctx = DispatchContext(
        cfg=CapabilityConfig(),
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
        router=router,
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
