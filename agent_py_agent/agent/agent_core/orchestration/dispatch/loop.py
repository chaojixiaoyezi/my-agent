

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_py_agent.agent.capability import CapabilityRouter
    from agent_py_agent.agent.capability.config import CapabilityConfig

    from ..subagents import SubAgent

from ....runtime_errors import runtime_error_report
from .no_progress import DispatchNoProgressTracker
from .params import DispatchExecutionPlan, DispatchParams, DispatchRuntimePolicy


@dataclass
class DispatchLoopParams:

    max_consecutive_rounds: int = 20
    execution_plan: DispatchExecutionPlan = field(default_factory=DispatchExecutionPlan)
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

    def __post_init__(self) -> None:
        self.max_runners = int(self.execution_plan.max_runners)


@dataclass
class DispatchLoopReport:

    rounds_count: int = 0
    total_records: int = 0
    final_pending_count: int = 0
    final_pending_load_error: dict[str, object] = field(default_factory=dict)
    stopped_by_limit: bool = False
    stopped_by_no_progress: bool = False
    rounds: list[dict] = field(default_factory=list)


_DISPATCH_LOOP_PARAM_KEYS = tuple(field.name for field in fields(DispatchLoopParams))


def _coerce_dispatch_loop_params(
    params: DispatchLoopParams | None,
    *,
    policy: DispatchRuntimePolicy | None = None,
    max_consecutive_rounds: int | None = None,
    apply: bool = False,
    execution_plan: DispatchExecutionPlan | None = None,
    planner: bool = False,
    workflow_mode: str = "off",
    max_runners: int | None = None,
    limit: int | None = None,
    reviewer: str = "parent-dispatch",
    note: str = "",
    runner_instruction: str = "",
    max_cards: int = 0,
    probe: bool = True,
    take_over_by: str = "",
    locked_files: list[str] | None = None,
) -> DispatchLoopParams:
    if params is not None and not isinstance(params, DispatchLoopParams):
        raise TypeError("dispatch_loop() requires params: DispatchLoopParams keyword argument")

    if params is not None:
        return params
    runtime_policy = policy or DispatchRuntimePolicy()
    return DispatchLoopParams(
        max_consecutive_rounds=_loop_round_limit(runtime_policy, max_consecutive_rounds),
        execution_plan=_loop_execution_plan(
            runtime_policy,
            apply=apply,
            execution_plan=execution_plan,
            max_runners=max_runners,
        ),
        planner=planner,
        workflow_mode=workflow_mode,
        max_runners=_loop_max_runners(runtime_policy, max_runners),
        limit=_loop_limit(runtime_policy, limit),
        reviewer=reviewer,
        note=note,
        runner_instruction=runner_instruction,
        max_cards=max_cards,
        probe=probe,
        take_over_by=take_over_by,
        locked_files=locked_files,
    )


def _loop_round_limit(policy: DispatchRuntimePolicy, value: int | None) -> int:
    return policy.max_consecutive_rounds if value is None else max(0, int(value))


def _loop_max_runners(policy: DispatchRuntimePolicy, value: int | None) -> int:
    return policy.default_max_runners if value is None else max(0, int(value))


def _loop_limit(policy: DispatchRuntimePolicy, value: int | None) -> int:
    return policy.default_limit if value is None else max(0, int(value))


def _loop_execution_plan(
    policy: DispatchRuntimePolicy,
    *,
    apply: bool,
    execution_plan: DispatchExecutionPlan | None,
    max_runners: int | None,
) -> DispatchExecutionPlan:
    return execution_plan or DispatchExecutionPlan.from_parts(
        mutate_state=apply,
        start_runners=False,
        max_runners=_loop_max_runners(policy, max_runners),
    )


def _run_single_dispatch(agent, routing: tuple[object, object], params: DispatchLoopParams):
    router, capability_config = routing
    return agent.dispatch_subagents(
        router,
        capability_config,
        params=_dispatch_params_from_loop(params),
    )


def _dispatch_params_from_loop(params: DispatchLoopParams) -> DispatchParams:
    return DispatchParams(
        execution_plan=params.execution_plan,
        planner=params.planner,
        workflow_mode=params.workflow_mode,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
        runner_instruction=params.runner_instruction,
        max_cards=params.max_cards,
        probe=params.probe,
        take_over_by=params.take_over_by,
        locked_files=params.locked_files,
    )


def _append_dispatch_round(report: DispatchLoopReport, dispatch_report, round_num: int) -> None:
    report.rounds_count = round_num
    report.total_records += len(dispatch_report.records)
    report.rounds.append(
        {
            "round": round_num,
            "record_count": len(dispatch_report.records),
            "ok": all(item.ok for item in dispatch_report.records),
        }
    )


def _dispatch_loop_params_from_locals(values: dict) -> DispatchLoopParams:
    selected = {
        key: values[key]
        for key in _DISPATCH_LOOP_PARAM_KEYS
        if key in values and (key != "locked_files" or values[key] is not None)
    }
    selected["apply"] = values.get("apply", False)
    selected["policy"] = DispatchRuntimePolicy.from_config(getattr(values.get("agent"), "config", None))
    return _coerce_dispatch_loop_params(values["params"], **selected)


def dispatch_loop(
    agent,
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None = None,
    *,
    params: DispatchLoopParams = None,
    max_consecutive_rounds: int | None = None,
    execution_plan: DispatchExecutionPlan | None = None,
    apply: bool = False,
    planner: bool = False,
    workflow_mode: str = "off",
    max_runners: int | None = None,
    limit: int | None = None,
    reviewer: str = "parent-dispatch",
    note: str = "",
    runner_instruction: str = "",
    max_cards: int = 0,
    probe: bool = True,
    take_over_by: str = "",
    locked_files: list[str] | None = None,
) -> DispatchLoopReport:
    params = _dispatch_loop_params_from_locals(locals())
    report = DispatchLoopReport()
    max_rounds = params.max_consecutive_rounds
    no_progress_tracker = DispatchNoProgressTracker()
    round_num = 0

    while max_rounds == 0 or round_num < max_rounds:
        round_num += 1
        dispatch_report = _run_single_dispatch(agent, (router, capability_config), params)
        _append_dispatch_round(report, dispatch_report, round_num)
        if not agent.has_pending_work:
            break
        if no_progress_tracker.should_stop(dispatch_report):
            report.stopped_by_no_progress = True
            _clear_pending_work(agent)
            break
        if max_rounds > 0 and round_num >= max_rounds:
            report.stopped_by_limit = True
            break

    report.final_pending_count, report.final_pending_load_error = _final_pending_runner_count(agent)
    return report


def _final_pending_runner_count(agent) -> tuple[int, dict[str, object]]:
    try:
        from ...runner.dispatch import (
            RunnerCandidatePolicy,
            _dispatch_runner_candidates,
            _runner_max_attempts,
            _same_run_redispatch_limit,
        )

        runtime_policy = getattr(agent, "runtime_guard_policy", None)
        runner_max_attempts = _runner_max_attempts(
            agent.config.runner_failure_policy,
            runtime_policy=runtime_policy,
        )
        same_run_limit = _same_run_redispatch_limit(
            getattr(agent.config, "same_run_redispatch_limit", None),
            runtime_policy=runtime_policy,
        )
        candidates = _dispatch_runner_candidates(
            agent.subagents.list_runs(),
            max_runners=999,
            policy=RunnerCandidatePolicy(
                runner_max_attempts=runner_max_attempts,
                same_run_redispatch_limit=same_run_limit,
            ),
        )
        return len(candidates), {}
    except Exception as exc:
        return 0, {
            "context": "dispatch_loop.final_pending_runner_count",
            "error": runtime_error_report(exc, context="dispatch_loop.final_pending_runner_count"),
        }


def _clear_pending_work(agent) -> None:
    try:
        agent._has_pending_work = False
    except Exception:
        return


__all__ = ["DispatchLoopReport", "DispatchLoopParams", "dispatch_loop"]
