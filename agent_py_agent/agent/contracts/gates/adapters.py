
from __future__ import annotations

from collections.abc import Iterable

from ..recovery_actions import RecoveryAction
from ..state_machine_transitions import transition_contract
from ..tool_name_resolution import suggested_tool_name
from ..tool_protocol_v2 import normalize_tool_call, validate_tool_call
from .models import GateDecision, GateFinding
from .tool.effects import ToolGatePolicy, tool_effect_decision


def evaluate_tool_call_gate(
    payload: object,
    *,
    available_tools: Iterable[str] | None = None,
    allowed_tools: Iterable[str] | None = None,
    policy: ToolGatePolicy | None = None,
) -> GateDecision:
    call = normalize_tool_call(_payload_for_tool_protocol(payload))
    findings = [GateFinding(_tool_protocol_code(code)) for code in validate_tool_call(call)]
    if findings:
        return GateDecision.repair(
            "tool_call",
            findings,
            recommended_action=RecoveryAction.REPAIR_TOOL_CALL.value,
        )
    available = _string_set(available_tools)
    allowed = _string_set(allowed_tools)
    if available is not None and call.tool_name not in available:
        evidence = {"tool_name": call.tool_name}
        if suggestion := suggested_tool_name(call.tool_name, available):
            evidence["suggested_tool_name"] = suggestion
        return GateDecision.deny("tool_call", "TOOL_NOT_REGISTERED", evidence=evidence)
    if allowed is not None and call.tool_name not in allowed:
        return GateDecision.deny("tool_call", "TOOL_NOT_ALLOWED", evidence={"tool_name": call.tool_name})
    effect_decision = tool_effect_decision(payload, call, policy)
    if effect_decision is not None and not effect_decision.allowed:
        return effect_decision
    return GateDecision.allow(
        "tool_call",
        evidence={
            "tool_name": call.tool_name,
            "operation_id": call.operation_id,
            "idempotency_key": call.idempotency_key,
            "schema_version": call.schema_version,
        },
    )


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


def _payload_for_tool_protocol(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    if "tool_name" in payload and "input" in payload:
        return payload
    if "tool" not in payload:
        return payload
    result = {
        "tool_name": str(payload.get("tool") or ""),
        "input": _runtime_tool_input(payload),
    }
    for key in ("schema_version", "operation_id", "idempotency_key", "artifact_refs", "metadata", "call_id"):
        if key in payload:
            result[key] = payload[key]
    return result


def _runtime_tool_input(payload: dict[str, object]) -> dict[str, object]:
    protocol_keys = {
        "artifact_refs",
        "call_id",
        "idempotency_key",
        "kind",
        "metadata",
        "operation_id",
        "run_id",
        "schema_version",
        "tool",
    }
    return {key: value for key, value in payload.items() if key not in protocol_keys}


def _tool_protocol_code(code: str) -> str:
    return "TOOL_PROTOCOL_" + str(code).upper()


def _string_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {str(item).strip() for item in values if str(item).strip()}


__all__ = ["evaluate_state_transition_gate", "evaluate_tool_call_gate"]
