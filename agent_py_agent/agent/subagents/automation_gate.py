
from __future__ import annotations

from dataclasses import asdict, dataclass, field

AUTO_SAFE_ACTIONS = frozenset({"query_recovery_tree", "inspect_refs"})


@dataclass(frozen=True)
class SubAgentAutomationGateRequest:
    mode: str = "manual"
    action: str = ""
    run_id: str = ""
    refs: list[str] = field(default_factory=list)
    mutates_task_state: bool = False
    executes_tools: bool = False
    manual_confirmed: bool = False
    recovery_candidate_count: int = 0
    max_recovery_candidates: int = 0
    reserved: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SubAgentAutomationGateResult:
    action: str
    mode: str
    execution_allowed: bool
    automatic_execution_allowed: bool
    guard_status: str
    blocked_by: list[str] = field(default_factory=list)
    recommended_next_step: str = ""
    reserved: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_subagent_automation_gate(request: SubAgentAutomationGateRequest) -> SubAgentAutomationGateResult:
    blockers = _blockers(request)
    if blockers:
        return SubAgentAutomationGateResult(
            action=request.action,
            mode=request.mode,
            execution_allowed=False,
            automatic_execution_allowed=False,
            guard_status="blocked",
            blocked_by=blockers,
            recommended_next_step="request_manual_confirmation",
            reserved={"refs_only": False, "auto_gate_version": 1},
        )
    if request.manual_confirmed:
        return SubAgentAutomationGateResult(
            action=request.action,
            mode=request.mode,
            execution_allowed=True,
            automatic_execution_allowed=False,
            guard_status="manual_confirmed",
            recommended_next_step="execute_with_audit",
            reserved={"refs_only": False, "auto_gate_version": 1},
        )
    auto_allowed = _is_auto_safe(request)
    return SubAgentAutomationGateResult(
        action=request.action,
        mode=request.mode,
        execution_allowed=auto_allowed,
        automatic_execution_allowed=auto_allowed,
        guard_status="allowed" if auto_allowed else "blocked",
        blocked_by=[] if auto_allowed else ["manual_confirmation_required"],
        recommended_next_step="continue_refs_only" if auto_allowed else "request_manual_confirmation",
        reserved={"refs_only": auto_allowed, "auto_gate_version": 1},
    )


def _blockers(request: SubAgentAutomationGateRequest) -> list[str]:
    blockers: list[str] = []
    if request.max_recovery_candidates > 0 and request.recovery_candidate_count > request.max_recovery_candidates:
        blockers.append("too_many_recovery_candidates")
    if not request.refs and request.action in AUTO_SAFE_ACTIONS:
        blockers.append("missing_refs")
    if not request.manual_confirmed:
        if request.mutates_task_state:
            blockers.append("mutates_task_state")
        if request.executes_tools:
            blockers.append("executes_tools")
    return blockers


def _is_auto_safe(request: SubAgentAutomationGateRequest) -> bool:
    return (
        str(request.mode or "").strip().lower() == "auto"
        and request.action in AUTO_SAFE_ACTIONS
        and bool(request.refs)
        and not request.mutates_task_state
        and not request.executes_tools
    )
