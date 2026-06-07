
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..models import GateDecision


@dataclass(frozen=True)
class ApprovalBindingFacts:
    tool_name: str
    run_id: str
    operation_id: str
    idempotency_key: str
    args_hash: str
    approved_actions: tuple[object, ...] = ()


def evaluate_approval_binding_gate(facts: ApprovalBindingFacts | Mapping[str, object]) -> GateDecision:
    item = _binding_facts(facts)
    for action in item.approved_actions:
        if not isinstance(action, Mapping):
            continue
        if not _base_identity_matches(action, item):
            continue
        if _binding_identity(action) == _binding_identity_from_facts(item):
            return GateDecision.allow(
                "approval_binding",
                evidence={"approval_id": str(action.get("approval_id") or "")},
            )
        return GateDecision.deny("approval_binding", "APPROVAL_BINDING_MISMATCH", evidence=_safe_action_evidence(action))
    return GateDecision.need_approval("approval_binding", "APPROVAL_REQUIRED", evidence={"tool_name": item.tool_name})


def approval_id_for_binding(facts: ApprovalBindingFacts | Mapping[str, object]) -> str:
    decision = evaluate_approval_binding_gate(facts)
    if decision.allowed:
        return str(decision.evidence.get("approval_id") or "")
    return ""


def _base_identity_matches(action: Mapping[object, object], facts: ApprovalBindingFacts) -> bool:
    status = str(action.get("status") or "").strip()
    if status and status != "APPROVED":
        return False
    tool = str(action.get("tool_name") or "").strip()
    key = str(action.get("idempotency_key") or "").strip()
    return bool(tool == facts.tool_name and key and key == facts.idempotency_key)


def _binding_identity(action: Mapping[object, object]) -> tuple[str, str, str, str]:
    return (
        str(action.get("run_id") or "").strip(),
        str(action.get("operation_id") or "").strip(),
        str(action.get("idempotency_key") or "").strip(),
        str(action.get("args_hash") or "").strip(),
    )


def _binding_identity_from_facts(facts: ApprovalBindingFacts) -> tuple[str, str, str, str]:
    return (
        str(facts.run_id or "").strip(),
        str(facts.operation_id or "").strip(),
        str(facts.idempotency_key or "").strip(),
        str(facts.args_hash or "").strip(),
    )


def _binding_facts(value: ApprovalBindingFacts | Mapping[str, object]) -> ApprovalBindingFacts:
    if isinstance(value, ApprovalBindingFacts):
        return value
    actions = value.get("approved_actions")
    return ApprovalBindingFacts(
        tool_name=str(value.get("tool_name") or ""),
        run_id=str(value.get("run_id") or ""),
        operation_id=str(value.get("operation_id") or ""),
        idempotency_key=str(value.get("idempotency_key") or ""),
        args_hash=str(value.get("args_hash") or ""),
        approved_actions=tuple(actions if isinstance(actions, Iterable) and not isinstance(actions, (str, bytes)) else ()),
    )


def _safe_action_evidence(action: Mapping[object, object]) -> dict[str, object]:
    return {
        "approval_id": str(action.get("approval_id") or ""),
        "tool_name": str(action.get("tool_name") or ""),
        "run_id": str(action.get("run_id") or ""),
        "operation_id": str(action.get("operation_id") or ""),
        "idempotency_key": str(action.get("idempotency_key") or ""),
        "args_hash": str(action.get("args_hash") or ""),
    }


__all__ = ["ApprovalBindingFacts", "approval_id_for_binding", "evaluate_approval_binding_gate"]
