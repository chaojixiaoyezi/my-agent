
from __future__ import annotations

from collections import defaultdict

from .models import GateContext, GateDecision, GateFinding, GateValidator


class GateRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, list[GateValidator]] = defaultdict(list)

    def register(self, phase: str, validator: GateValidator) -> None:
        self._validators[str(phase)].append(validator)

    def has_validators(self, phase: str) -> bool:
        return bool(self._validators.get(str(phase)))

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


def _merged_status(decisions: list[GateDecision]) -> str:
    for status in ("DENY", "BLOCKED", "NEED_APPROVAL", "NEED_REPAIR", "RECOVERING"):
        if any(decision.status == status for decision in decisions):
            return status
    return decisions[0].status if decisions else "DENY"


def _merged_action(decisions: list[GateDecision]) -> str:
    for decision in decisions:
        if decision.recommended_action:
            return decision.recommended_action
    return "stop"


__all__ = ["GateRegistry"]
