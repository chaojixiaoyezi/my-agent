
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..capabilities import CapabilityRouter
    from ..capability_config import CapabilityConfig
    from ..subagent import SubAgent


@dataclass
class DispatchLoopParams:

    max_consecutive_rounds: int = 20
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
class DispatchLoopReport:

    rounds_count: int = 0
    total_records: int = 0
    final_pending_count: int = 0
    stopped_by_limit: bool = False
    rounds: list[dict] = field(default_factory=list)


_DISPATCH_LOOP_PARAM_KEYS = [
    "max_consecutive_rounds", "apply", "execute_runners", "planner",
    "workflow_mode", "max_runners", "limit", "reviewer", "note",
    "runner_instruction", "max_cards", "probe", "take_over_by", "locked_files",
]


def _coerce_dispatch_loop_params(params, kwargs) -> DispatchLoopParams:
    if params is None:
        params = DispatchLoopParams()
    elif not isinstance(params, DispatchLoopParams):
        raise TypeError("dispatch_loop() requires params: DispatchLoopParams keyword argument")

    for key in _DISPATCH_LOOP_PARAM_KEYS:
        if key in kwargs:
            setattr(params, key, kwargs[key])
    return params


def _run_single_dispatch(agent, router, capability_config, params):
    return agent.dispatch_subagents(
        router,
        capability_config,
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
    )


def dispatch_loop(
    agent,
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None = None,
    *,
    params: DispatchLoopParams = None,
    **kwargs,
) -> DispatchLoopReport:
    params = _coerce_dispatch_loop_params(params, kwargs)
    report = DispatchLoopReport()
    max_rounds = params.max_consecutive_rounds

    for round_num in range(1, max_rounds + 1):
        dispatch_report = _run_single_dispatch(
            agent, router, capability_config, params
        )
        report.rounds_count = round_num
        report.total_records += len(dispatch_report.records)
        report.rounds.append({
            "round": round_num,
            "record_count": len(dispatch_report.records),
            "ok": all(item.ok for item in dispatch_report.records),
        })
        if not agent.has_pending_work:
            break
        if round_num >= max_rounds:
            report.stopped_by_limit = True
            break

    report.final_pending_count = _final_pending_runner_count(agent)
    return report


def _final_pending_runner_count(agent) -> int:
    try:
        from .runner_dispatch import _dispatch_runner_candidates, _runner_max_attempts

        runner_max_attempts = _runner_max_attempts(agent.config.runner_failure_policy)
        candidates = _dispatch_runner_candidates(
            agent.subagents.list_runs(),
            max_runners=999,
            runner_max_attempts=runner_max_attempts,
        )
        return len(candidates)
    except Exception:
        return 0


__all__ = ["DispatchLoopReport", "DispatchLoopParams", "dispatch_loop"]
