
from __future__ import annotations

from ..state_machine_transitions import transition_contract
from .models import GateDecision


def evaluate_state_transition_gate(from_status: object, to_status: object) -> GateDecision:
    contract = transition_contract(from_status, to_status)
    if contract.allowed:
        return GateDecision.allow(
            "state_transition",
            evidence={
                "from_status": contract.from_status,
                "to_status": contract.to_status,
                "required_condition": contract.required_condition,
            },
        )
    return GateDecision.deny(
        "state_transition",
        "STATE_TRANSITION_DISALLOWED",
        evidence={
            "from_status": contract.from_status,
            "to_status": contract.to_status,
            "reason": contract.reason,
            "required_condition": contract.required_condition,
        },
    )


__all__ = [
    "evaluate_state_transition_gate",
]
