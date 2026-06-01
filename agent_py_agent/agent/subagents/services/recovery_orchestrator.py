# LLM: Recovery orchestrator turns strategy advice into an auditable control-plane plan.
# 模块用途: 串联子代理恢复策略、可执行接管动作和恢复账本，不把建议散落到多个调用点。

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ...io import append_jsonl
from .recovery_strategy import (
    SubagentRecoveryStrategy,
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)
from .takeover_run import TakeoverRunRequest

SCHEMA_VERSION = "subagent_recovery_orchestration.v1"


@dataclass(frozen=True)
class RecoveryOrchestrationRequest:
    """Scope and mode for one recovery orchestration pass."""

    run_ids: list[str] = field(default_factory=list)
    strategies: list[SubagentRecoveryStrategy] = field(default_factory=list)
    apply: bool = False
    requested_by: str = "parent"
    max_items: int = 20
    now: float = 0.0


@dataclass(frozen=True)
class RecoveryOrchestrationStep:
    """One recovery decision plus any controlled side effect that actually ran."""

    run_id: str
    recommended_action: str
    orchestration_action: str
    applied: bool
    ok: bool
    requires_dispatch: bool = False
    requires_human: bool = False
    message: str = ""
    suggested_tool_call: dict[str, object] = field(default_factory=dict)
    result_refs: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RecoveryOrchestrationReport:
    """Aggregate result for a recovery orchestration pass."""

    schema_version: str
    generated_at: float
    requested_by: str
    dry_run: bool
    summary: dict[str, int]
    steps: list[RecoveryOrchestrationStep]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SubAgentRecoveryOrchestrator:
    """Coordinate recovery strategies through one refs-only ledger."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def orchestrate(self, request: RecoveryOrchestrationRequest) -> RecoveryOrchestrationReport:
        strategies = _strategies_for_request(self.manager, request)
        steps = [_step_for_strategy(self.manager, strategy, request) for strategy in strategies]
        report = RecoveryOrchestrationReport(
            schema_version=SCHEMA_VERSION,
            generated_at=request.now or time.time(),
            requested_by=request.requested_by,
            dry_run=not request.apply,
            summary=_summary(steps),
            steps=steps,
        )
        _append_ledger(self.manager, report)
        return report


def _strategies_for_request(manager: Any, request: RecoveryOrchestrationRequest) -> list[SubagentRecoveryStrategy]:
    if request.strategies:
        return list(request.strategies)[: _positive_limit(request.max_items)]
    run_ids = [item for item in request.run_ids if item]
    if not run_ids:
        run_ids = _recoverable_run_ids(manager, _positive_limit(request.max_items))
    try:
        all_tasks = manager.list_runs()
    except Exception:
        all_tasks = []
    strategies: list[SubagentRecoveryStrategy] = []
    for run_id in run_ids[: _positive_limit(request.max_items)]:
        try:
            task = manager.load(run_id)
        except FileNotFoundError:
            continue
        strategies.append(
            build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task, all_tasks=all_tasks))
        )
    return strategies


def _recoverable_run_ids(manager: Any, limit: int) -> list[str]:
    try:
        tasks = manager.list_runs()
    except Exception:
        return []
    ids: list[str] = []
    for task in tasks:
        status = str(getattr(task, "status", "") or "").upper()
        if status in {"BLOCKED", "FAILED", "TIMEOUT", "ERROR", "CHANNEL_ERROR"}:
            ids.append(str(getattr(task, "id", "") or ""))
    return [item for item in ids if item][:limit]


def _step_for_strategy(
    manager: Any,
    strategy: SubagentRecoveryStrategy,
    request: RecoveryOrchestrationRequest,
) -> RecoveryOrchestrationStep:
    action = strategy.recommended_action
    if action.startswith("rerun_original"):
        return _dispatch_step(strategy)
    if action.startswith("create_takeover_run"):
        return _takeover_step(manager, strategy, request)
    if action == "recover_coordinator_leadership":
        return _leadership_step(strategy)
    if action == "closed_no_action":
        return _noop_step(strategy)
    return _manual_step(strategy)


def _dispatch_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    call: dict[str, object] = {
        "tool": "dispatch_subagents",
        "dry_run": False,
        "run_ids": [strategy.run_id],
        "workflow_mode": "off",
    }
    if strategy.runner_instruction:
        call["runner_instruction"] = strategy.runner_instruction
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="dispatch_original_run",
        applied=False,
        ok=True,
        requires_dispatch=True,
        message="原 run 可续跑；已生成统一 dispatch 建议，等待父代理或调度器执行。",
        suggested_tool_call=call,
        result_refs=[item for item in [strategy.packet_ref, *strategy.fallback_refs] if item],
        blocked_by=list(strategy.blocked_by),
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
            message="dry-run: 可通过 apply=True 创建或复用 takeover run。",
            result_refs=[*strategy.takeover_refs, strategy.packet_ref],
            blocked_by=list(strategy.blocked_by),
        )
    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=strategy.run_id, reason=strategy.recommended_action))
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="create_takeover_run",
        applied=bool(result.applied),
        ok=bool(result.takeover_run_id or result.applied),
        message=result.message,
        result_refs=[item for item in [result.takeover_run_id, *result.takeover_refs.values()] if item],
        blocked_by=list(strategy.blocked_by),
    )


def _leadership_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="plan_leadership_recovery",
        applied=False,
        ok=True,
        requires_human=True,
        message="coordinator/lead 失联需要明确新 leader；先生成 leadership recovery plan，再按子集 apply。",
        suggested_tool_call={
            "tool": "inspect_agent_tree",
            "root_id": strategy.run_id,
            "include_recovery": True,
        },
        result_refs=[strategy.packet_ref, *strategy.fallback_refs],
        blocked_by=list(strategy.blocked_by),
    )


def _noop_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="none",
        applied=False,
        ok=True,
        message="任务已关闭，无需恢复动作。",
    )


def _manual_step(strategy: SubagentRecoveryStrategy) -> RecoveryOrchestrationStep:
    return RecoveryOrchestrationStep(
        run_id=strategy.run_id,
        recommended_action=strategy.recommended_action,
        orchestration_action="manual_review",
        applied=False,
        ok=False,
        requires_human=True,
        message="缺少可安全自动执行的恢复入口；请读 refs 后由父级或用户决定。",
        result_refs=[strategy.packet_ref, *strategy.fallback_refs],
        blocked_by=list(strategy.blocked_by),
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


def _append_ledger(manager: Any, report: RecoveryOrchestrationReport) -> None:
    path = manager.workspace / "subagent_recovery_ledger.jsonl"
    for step in report.steps:
        payload = {
            "schema_version": report.schema_version,
            "generated_at": report.generated_at,
            "requested_by": report.requested_by,
            "dry_run": report.dry_run,
            **asdict(step),
        }
        append_jsonl(path, payload, sort_keys=True)


def _positive_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 20
    return parsed if parsed > 0 else 20


__all__ = [
    "RecoveryOrchestrationReport",
    "RecoveryOrchestrationRequest",
    "RecoveryOrchestrationStep",
    "SCHEMA_VERSION",
    "SubAgentRecoveryOrchestrator",
]
