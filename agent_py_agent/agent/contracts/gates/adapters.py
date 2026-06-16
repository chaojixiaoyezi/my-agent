
from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher

from ..recovery import RecoveryAction
from ..state_machine_transitions import transition_contract
from ..tool_call_policy import ToolCallPolicy, validate_tool_call_policy
from ..tool_protocol_v2 import normalize_tool_call, validate_tool_call
from .models import GateDecision, GateFinding
from .tool_effects import ToolGatePolicy, tool_effect_decision

_SUGGESTION_THRESHOLD = 0.74


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


_PARAMETER_ERROR_CODES = {"TOOL_PARAMETER_REQUIRED", "TOOL_PARAMETER_TYPE_INVALID"}


def evaluate_tool_call_parameter_gate(
    payload: object,
    tool_call_policy: ToolCallPolicy | None,
) -> GateDecision:
    """灰度 required + 顶层 type 校验：缺参/类型错 -> repair(精确 code + findings)。

    与结构校验 evaluate_tool_call_gate 同处 tool_call 门位置（manifest/effect/execute 之前），
    native 与 text 两条入口都经此（payload 先归一为 v2 envelope）。
    只对 _PARAMETER_ERROR_CODES 生效（required/顶层 type）；不碰 enum/minimum，
    也不重复 policy 内的 available/allowed（那两条由 evaluate_tool_call_gate 负责）。
    policy 为 None 或无声明时直接 allow（零开销、零误拒）。
    """
    if tool_call_policy is None:
        return GateDecision.allow("tool_call")
    call = normalize_tool_call(_payload_for_tool_protocol(payload))
    decision = validate_tool_call_policy(call, tool_call_policy)
    if decision.ok or decision.error_code not in _PARAMETER_ERROR_CODES:
        return GateDecision.allow("tool_call")
    finding = GateFinding(
        decision.error_code,
        evidence={"tool_name": decision.tool_name, "fields": list(decision.findings)},
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
]
