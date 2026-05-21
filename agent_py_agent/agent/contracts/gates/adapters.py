# LLM: Gate adapters connect protocol/state contracts to mandatory runtime checkpoints.
# 模块用途: 保留轻量入口，把工具协议和状态机合同转成统一 GateDecision。

from __future__ import annotations

from collections.abc import Iterable

from ..state_machine_transitions import transition_contract
from ..tool_protocol_v2 import normalize_tool_call, validate_tool_call
from .models import GateDecision, GateFinding
from .tool_effects import ToolGatePolicy, tool_effect_decision


# LLM: evaluate_tool_call_gate validates protocol, allowlist, and optional side-effect policy.
# 函数用途: 在工具执行前校验 tool call 结构、注册表/allowlist 和副作用门。
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
        return GateDecision.repair("tool_call", findings, recommended_action="repair_tool_call")
    available = _string_set(available_tools)
    allowed = _string_set(allowed_tools)
    if available is not None and call.tool_name not in available:
        return GateDecision.deny("tool_call", "TOOL_NOT_REGISTERED", evidence={"tool_name": call.tool_name})
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


# LLM: evaluate_state_transition_gate wraps the shared lifecycle transition table.
# 函数用途: 判断 from/to 状态是否满足状态机合同，禁止绕过 VERIFYING 直接完成。
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


# LLM: _payload_for_tool_protocol adapts legacy tool dicts into protocol input shape.
# 函数用途: 只按机器字段 tool/args/input 归一化，不解析自然语言正文。
def _payload_for_tool_protocol(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    if "tool" not in payload or any(key in payload for key in ("args", "arguments", "input")):
        return payload
    args = {key: value for key, value in payload.items() if key not in {"tool", "kind"}}
    return {**payload, "args": args}


# LLM: _tool_protocol_code maps protocol validator codes into gate finding codes.
# 函数用途: 给 tool_protocol_v2 的错误码加稳定前缀，避免调用方混淆来源。
def _tool_protocol_code(code: str) -> str:
    return "TOOL_PROTOCOL_" + str(code).upper()


# LLM: _string_set normalizes optional tool-name collections.
# 函数用途: 将 available/allowed 工具列表整理成非空字符串集合。
def _string_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {str(item).strip() for item in values if str(item).strip()}


__all__ = ["evaluate_state_transition_gate", "evaluate_tool_call_gate"]
