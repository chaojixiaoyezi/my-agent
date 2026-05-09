# LLM: Subagent automation gate separates refs-only automation from state-changing execution.
# 模块用途: 判断某个子代理自动化动作能否执行；默认保守，防止无人值守时误跑工具或改任务状态。

from __future__ import annotations

from dataclasses import asdict, dataclass, field

AUTO_SAFE_ACTIONS = frozenset({"query_recovery_tree", "inspect_refs"})


# LLM: SubAgentAutomationGateRequest is the bundle future auto runners must pass through.
# 类用途: 描述自动化动作的模式、风险属性、refs 和恢复候选数量。
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


# LLM: SubAgentAutomationGateResult exposes both current allowance and true automatic allowance.
# 类用途: 返回动作是否可执行、是否算无人值守自动放行、阻断原因和下一步建议。
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

    # LLM: to_dict supports audit JSON without callers depending on dataclass internals.
    # 函数用途: 输出 JSON 友好的自动化门结果。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: evaluate_subagent_automation_gate is the deterministic policy entrypoint.
# 函数用途: 根据动作类型和风险属性判断是否放行；不执行动作、不修改任务。
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


# LLM: _blockers returns hard blockers that apply before manual or automatic allowance.
# 函数用途: 识别会让动作不能继续的确定性风险，例如缺 refs 或候选过多。
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


# LLM: _is_auto_safe allows only explicit refs-only actions in auto mode.
# 函数用途: 判断动作是否属于无人值守自动可执行的只读范围。
def _is_auto_safe(request: SubAgentAutomationGateRequest) -> bool:
    return (
        str(request.mode or "").strip().lower() == "auto"
        and request.action in AUTO_SAFE_ACTIONS
        and bool(request.refs)
        and not request.mutates_task_state
        and not request.executes_tools
    )
