# LLM: GateRegistry composes mandatory contract gates for one runtime phase.
# 模块用途: 让调用方按 phase 注册/执行 gate，并用统一策略合并阻断结果。

from __future__ import annotations

from collections import defaultdict

from .models import GateContext, GateDecision, GateFinding, GateValidator


# LLM: GateRegistry keeps gate orchestration tiny and deterministic.
# 类用途: 根据 phase 顺序执行 gate；任一 gate 阻断时返回合并后的结构化 findings。
class GateRegistry:
    # LLM: GateRegistry.__init__ initializes phase-indexed validators.
    # 函数用途: 创建按 phase 分组的 gate validator 表。
    def __init__(self) -> None:
        self._validators: dict[str, list[GateValidator]] = defaultdict(list)

    # LLM: GateRegistry.register appends one validator to a runtime phase.
    # 函数用途: 注册可组合 gate，不改变已有 phase 顺序。
    def register(self, phase: str, validator: GateValidator) -> None:
        self._validators[str(phase)].append(validator)

    # LLM: GateRegistry.has_validators reports whether a gate/phase is actually wired.
    # 函数用途: 供上层 pipeline 区分“无注册必须失败关闭”和“注册后执行结果允许”。
    def has_validators(self, phase: str) -> bool:
        return bool(self._validators.get(str(phase)))

    # LLM: GateRegistry.evaluate runs phase validators and merges blocking decisions.
    # 函数用途: 执行一个 phase 的所有 gate，任一失败则返回合并 finding。
    def evaluate(self, context: GateContext) -> GateDecision:
        decisions = [validator(context) for validator in self._validators.get(context.phase, [])]
        if not decisions:
            return GateDecision.allow(context.phase, evidence={"gate_count": 0})
        blocking = [decision for decision in decisions if not decision.allowed]
        if not blocking:
            return GateDecision.allow(
                context.phase,
                evidence={"gate_count": len(decisions), "gates": [item.gate for item in decisions]},
            )
        if len(blocking) == 1:
            return blocking[0]
        findings: list[GateFinding] = []
        for decision in blocking:
            findings.extend(decision.findings)
        status = _merged_status(blocking)
        return GateDecision(
            gate=context.phase,
            status=status,
            allowed=False,
            findings=tuple(findings),
            recommended_action=_merged_action(blocking),
            evidence={"gate_count": len(decisions), "blocked_gates": [item.gate for item in blocking]},
        )


# LLM: _merged_status chooses the strongest blocking status from child gate decisions.
# 函数用途: 合并 DENY/BLOCKED/NEED_APPROVAL/NEED_REPAIR/RECOVERING 的优先级。
def _merged_status(decisions: list[GateDecision]) -> str:
    for status in ("DENY", "BLOCKED", "NEED_APPROVAL", "NEED_REPAIR", "RECOVERING"):
        if any(decision.status == status for decision in decisions):
            return status
    return decisions[0].status if decisions else "DENY"


# LLM: _merged_action picks the first structured recovery action from blocking gates.
# 函数用途: 给调用方提供下一步动作，不解析 finding message。
def _merged_action(decisions: list[GateDecision]) -> str:
    for decision in decisions:
        if decision.recommended_action:
            return decision.recommended_action
    return "stop"


__all__ = ["GateRegistry"]
