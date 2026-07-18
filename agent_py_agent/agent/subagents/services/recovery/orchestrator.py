
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ....io import append_jsonl
from ....runtime_errors import runtime_error_report
from ...models import SUBAGENT_FAILURE_STATUSES, task_status_in
from ..takeover.run import TakeoverRunRequest
from .modes import RecoveryMode, is_rerun_mode, is_takeover_mode, recovery_mode_or_manual
from .strategy import (
    SubagentRecoveryStrategy,
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)

SCHEMA_VERSION = "subagent_recovery_orchestration.v1"


@dataclass(frozen=True)
class RecoveryOrchestrationRequest:
    run_ids: list[str] = field(default_factory=list)
    strategies: list[SubagentRecoveryStrategy] = field(default_factory=list)
    apply: bool = False
    requested_by: str = "parent"
    max_items: int = 20
    now: float = 0.0


@dataclass(frozen=True)
class RecoveryOrchestrationStep:
    run_id: str
    recommended_action: str
    orchestration_action: str
    applied: bool
    ok: bool
    next_actor: str = "parent_agent"
    requires_dispatch: bool = False
    requires_human: bool = False
    message: str = ""
    suggested_tool_call: dict[str, object] = field(default_factory=dict)
    result_refs: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    strategy_snapshot: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryOrchestrationReport:
    schema_version: str
    generated_at: float
    requested_by: str
    dry_run: bool
    summary: dict[str, int]
    steps: list[RecoveryOrchestrationStep]
    load_errors: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SubAgentRecoveryOrchestrator:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def orchestrate(self, request: RecoveryOrchestrationRequest) -> RecoveryOrchestrationReport:
        strategies, load_errors = _strategies_for_request(self.manager, request)
        steps = [_step_for_strategy(self.manager, strategy, request) for strategy in strategies]
        report = RecoveryOrchestrationReport(
            schema_version=SCHEMA_VERSION,
            generated_at=request.now or time.time(),
            requested_by=request.requested_by,
            dry_run=not request.apply,
            summary=_summary(steps),
            steps=steps,
            load_errors=load_errors,
        )
        _append_ledger(self.manager, report)
        return report


def _strategies_for_request(
    manager: Any,
    request: RecoveryOrchestrationRequest,
) -> tuple[list[SubagentRecoveryStrategy], list[dict[str, object]]]:
    load_errors: list[dict[str, object]] = []
    if request.strategies:
        return list(request.strategies)[: _positive_limit(request.max_items)], load_errors
    run_ids = [item for item in request.run_ids if item]
    if not run_ids:
        run_ids, recoverable_errors = _recoverable_run_ids(manager, _positive_limit(request.max_items))
        load_errors.extend(recoverable_errors)
    try:
        all_tasks = manager.list_runs()
    except Exception as exc:
        all_tasks = []
        load_errors.append(_runtime_load_error(exc, "subagent_recovery_orchestration.list_all_tasks"))
    strategies: list[SubagentRecoveryStrategy] = []
    for run_id in run_ids[: _positive_limit(request.max_items)]:
        try:
            task = manager.load(run_id)
        except FileNotFoundError:
            continue
        except Exception as exc:
            load_errors.append(_runtime_load_error(exc, "subagent_recovery_orchestration.load_task", run_id=run_id))
            continue
        strategies.append(
            build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task, all_tasks=all_tasks))
        )
    return strategies, load_errors


def _recoverable_run_ids(manager: Any, limit: int) -> tuple[list[str], list[dict[str, object]]]:
    try:
        tasks = manager.list_runs()
    except Exception as exc:
        return [], [_runtime_load_error(exc, "subagent_recovery_orchestration.list_recoverable_runs")]
    ids: list[str] = []
    for task in tasks:
        if task_status_in(getattr(task, "status", ""), SUBAGENT_FAILURE_STATUSES):
            ids.append(str(getattr(task, "id", "") or ""))
    return [item for item in ids if item][:limit], []


def _step_for_strategy(
    manager: Any,
    strategy: SubagentRecoveryStrategy,
    request: RecoveryOrchestrationRequest,
) -> RecoveryOrchestrationStep:
    mode = recovery_mode_or_manual(getattr(strategy, "recovery_mode", None))
    if is_rerun_mode(mode):
        return _dispatch_step(strategy)
    if is_takeover_mode(mode):
        return _takeover_step(manager, strategy, request)
    if mode is RecoveryMode.LEADERSHIP_RECOVERY:
        return _leadership_step(strategy)
    if mode is RecoveryMode.CLOSED:
        return _noop_step(strategy)
    return _manual_step(strategy)


def _dispatch_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    call: dict[str, object] = {
        "tool": "dispatch_subagents",
        "dry_run": False,
        "run_ids": [strategy.run_id],
        "recovery_mode": strategy.recovery_mode,
    }
    if strategy.runner_instruction:
        call["runner_instruction"] = strategy.runner_instruction
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="dispatch_original_run",
        applied=False,
        ok=True,
        next_actor="dispatcher",
        requires_dispatch=True,
        message="原 run 可续跑；已生成统一 dispatch 建议，等待父代理或调度器执行。",
        suggested_tool_call=call,
        result_refs=[item for item in [strategy.packet_ref, *strategy.recovery_refs] if item],
        blocked_by=list(strategy.blocked_by),
        strategy_snapshot=_strategy_snapshot(strategy),
    )


def _takeover_step(
    manager: Any,
    strategy: SubagentRecoveryStrategy,
    request: RecoveryOrchestrationRequest,
) -> RecoveryOrchestrationStep:
    if not request.apply:
        return RecoveryOrchestrationStep(
            run_id=strategy.run_id,
            recommended_action=strategy.recommended_action,
            orchestration_action="create_takeover_run",
            applied=False,
            ok=True,
            next_actor="orchestrator",
            message="dry-run: 可通过 apply=True 创建或复用 takeover run。",
            result_refs=[*strategy.takeover_refs, strategy.packet_ref],
            blocked_by=list(strategy.blocked_by),
            strategy_snapshot=_strategy_snapshot(strategy),
        )
    result = manager.create_takeover_run(
        TakeoverRunRequest(
            source_run_id=strategy.run_id,
            reason=strategy.recovery_mode or strategy.recommended_action,
        )
    )
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="create_takeover_run",
        applied=bool(result.applied),
        ok=bool(result.takeover_run_id or result.applied),
        next_actor="orchestrator",
        message=result.message,
        result_refs=[item for item in [result.takeover_run_id, *result.takeover_refs.values()] if item],
        blocked_by=list(strategy.blocked_by),
        strategy_snapshot=_strategy_snapshot(strategy),
    )


def _leadership_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="plan_leadership_recovery",
        applied=False,
        ok=True,
        next_actor="parent_agent",
        requires_human=True,
        message="coordinator/lead 失联需要明确新 leader；先生成 leadership recovery plan，再按子集 apply。",
        suggested_tool_call={
            "tool": "inspect_agent_tree",
            "root_id": strategy.run_id,
            "include_recovery": True,
        },
        result_refs=[strategy.packet_ref, *strategy.recovery_refs],
        blocked_by=list(strategy.blocked_by),
        strategy_snapshot=_strategy_snapshot(strategy),
    )


def _noop_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="none",
        applied=False,
        ok=True,
        next_actor="none",
        message="任务已关闭，无需恢复动作。",
        strategy_snapshot=_strategy_snapshot(strategy),
    )


def _manual_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="manual_review",
        applied=False,
        ok=False,
        next_actor="human_or_parent_agent",
        requires_human=True,
        message="缺少可安全自动执行的恢复入口；请读 refs 后由父级或用户决定。",
        result_refs=[strategy.packet_ref, *strategy.recovery_refs],
        blocked_by=list(strategy.blocked_by),
        strategy_snapshot=_strategy_snapshot(strategy),
    )


def _summary(steps: list[RecoveryOrchestrationStep]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(steps)}
    for step in steps:
        summary[step.orchestration_action] = summary.get(step.orchestration_action, 0) + 1
        summary["applied" if step.applied else "not_applied"] = summary.get(
            "applied" if step.applied else "not_applied", 0
        ) + 1
        if step.requires_human:
            summary["requires_human"] = summary.get("requires_human", 0) + 1
        if step.requires_dispatch:
            summary["requires_dispatch"] = summary.get("requires_dispatch", 0) + 1
    return summary


def _runtime_load_error(exc: BaseException, context: str, *, run_id: str = "") -> dict[str, object]:
    payload: dict[str, object] = {
        "context": context,
        "error": runtime_error_report(exc, context=context),
    }
    if run_id:
        payload["run_id"] = run_id
    return payload


def _append_ledger(manager: Any, report: RecoveryOrchestrationReport) -> None:
    path = manager.workspace / "subagent_recovery_ledger.jsonl"
    for index, step in enumerate(report.steps, start=1):
        payload = {
            "schema_version": report.schema_version,
            "generated_at": report.generated_at,
            "requested_by": report.requested_by,
            "dry_run": report.dry_run,
            "step_index": index,
            **asdict(step),
        }
        append_jsonl(path, payload, sort_keys=True)


def _strategy_snapshot(strategy: SubagentRecoveryStrategy) -> dict[str, object]:
    """Small immutable breadcrumb for debugging why a recovery action was chosen."""

    return {
        "run_id": strategy.run_id,
        "status": strategy.status,
        "role": strategy.role,
        "packet_status": strategy.packet_status,
        "packet_ref": strategy.packet_ref,
        "uses_continue_packet": strategy.uses_continue_packet,
        "memory_scope": strategy.memory_scope,
        "recovery_refs": list(strategy.recovery_refs),
        "takeover_refs": list(strategy.takeover_refs),
        "child_run_ids": list(strategy.child_run_ids),
        "leadership_recovery": strategy.leadership_recovery,
        "no_progress_fuse": strategy.no_progress_fuse,
    }


def _positive_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 20
    return parsed if parsed > 0 else 20
