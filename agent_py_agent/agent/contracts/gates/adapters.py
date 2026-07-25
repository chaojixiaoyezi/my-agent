
from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher

from ..recovery import RecoveryAction
from ..state_machine_transitions import transition_contract
from ..tool_call_policy import ToolCallPolicy, validate_tool_call_policy
from ..tool_protocol_v2 import (
    execution_payload_for_tool_protocol,
    normalize_tool_call,
    validate_tool_call,
)
from .models import GateDecision, GateFinding
from .tool_effects import ToolGatePolicy, tool_effect_decision

_SUGGESTION_THRESHOLD = 0.74


def evaluate_tool_call_gate(
    payload: object,
    *,
    available_tools: Iterable[str] | None = None,
    allowed_tools: Iterable[str] | None = None,
    policy: ToolGatePolicy | None = None,
    declared_input_fields: tuple[str, ...] = (),
) -> GateDecision:
    call = normalize_tool_call(
        execution_payload_for_tool_protocol(
            payload,
            declared_input_fields=declared_input_fields,
        )
    )
    findings = [_tool_protocol_finding(code) for code in validate_tool_call(call)]
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


_PARAMETER_ERROR_CODES = {
    "TOOL_INVALID_ARGUMENTS",
    "TOOL_PARAMETER_REQUIRED",
    "TOOL_PARAMETER_TYPE_INVALID",
}


def is_tool_parameter_error_code(value: object) -> bool:
    """Return whether a gate finding belongs to the canonical input-schema layer."""

    return str(value or "").strip().upper() in _PARAMETER_ERROR_CODES


def evaluate_tool_call_parameter_gate(
    payload: object,
    tool_call_policy: ToolCallPolicy | None,
) -> GateDecision:
    """LLM: 参数门消费 canonical Schema 的结构化问题，native/text 入口不得各自放宽。

    函数用途: 在工具副作用发生前，把缺参、类型和其他 Schema 约束错误转成可修复运行门结果。
    """
    if tool_call_policy is None:
        return GateDecision.allow("tool_call")
    tool_name = str(payload.get("tool") or "") if isinstance(payload, dict) else ""
    schema = tool_call_policy.input_schemas.get(tool_name)
    declared_fields = tuple(
        str(key)
        for key in (schema.get("properties") or {})
        if isinstance(schema, dict)
    )
    call = normalize_tool_call(
        execution_payload_for_tool_protocol(
            payload,
            declared_input_fields=declared_fields,
        )
    )
    decision = validate_tool_call_policy(call, tool_call_policy)
    if decision.ok or decision.error_code not in _PARAMETER_ERROR_CODES:
        return GateDecision.allow("tool_call")
    finding = GateFinding(
        decision.error_code,
        evidence={
            "tool_name": decision.tool_name,
            "fields": list(decision.findings),
            "issues": [dict(item) for item in decision.issues],
        },
    )
    return GateDecision.repair(
        "tool_call",
        (finding,),
        recommended_action=RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
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


def _tool_protocol_finding(code: str) -> GateFinding:
    raw = str(code or "").strip()
    if raw.startswith("artifact_refs["):
        return GateFinding("TOOL_PROTOCOL_ARTIFACT_REF_INVALID", evidence={"finding": raw})
    return GateFinding("TOOL_PROTOCOL_" + raw.upper())


def _string_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {str(item).strip() for item in values if str(item).strip()}


def suggested_tool_name(raw_name: object, available_tools: Iterable[str] | None) -> str:
    """Return a single close suggestion for diagnostics; callers must not execute it automatically."""
    raw = _normalized_tool_key(str(raw_name or ""))
    available = _available_tool_names(available_tools)
    if not raw or not available:
        return ""
    scored = sorted(
        (
            (SequenceMatcher(None, raw, _normalized_tool_key(name)).ratio(), name)
            for name in available
        ),
        reverse=True,
    )
    if not scored or scored[0][0] < _SUGGESTION_THRESHOLD:
        return ""
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return ""
    return scored[0][1]


def _available_tool_names(values: Iterable[str] | None) -> set[str]:
    return {str(item).strip() for item in values or [] if str(item).strip()}


def _normalized_tool_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(".", "_").replace("/", "_")


__all__ = [
    "evaluate_state_transition_gate",
    "evaluate_tool_call_gate",
    "evaluate_tool_call_parameter_gate",
    "is_tool_parameter_error_code",
]
