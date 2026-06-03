
from __future__ import annotations

from typing import Any

from ..action_protocol import ToolCallEnvelope
from ..contracts.gates import GateDecision
from .models import ToolExecutionResult
from .registry_envelopes import attach_result_envelope


def runtime_gate_block_result(
    payload: dict[str, Any],
    decision: GateDecision,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult:
    result = ToolExecutionResult(
        str(decision.evidence.get("tool_name") or payload.get("tool") or "unknown"),
        False,
        _gate_output(decision),
        error_code=decision.finding_codes[0] if decision.finding_codes else "RUNTIME_GATE_DENIED",
    )
    result = attach_result_envelope(result, envelope)
    attach_runtime_gate(result, decision)
    return result


def attach_runtime_gate(result: ToolExecutionResult, decision: GateDecision) -> None:
    envelope = dict(result.result_envelope or {})
    envelope["runtime_gate"] = decision.to_dict()
    result.result_envelope = envelope


def _gate_output(decision: GateDecision) -> str:
    codes = ",".join(decision.finding_codes) or "RUNTIME_GATE_DENIED"
    hint = " 路径超出允许的工作区范围，请使用工作区内或已授权 root 下的路径。" if _has_path_finding(decision) else ""
    model_message = decision.model_message
    if model_message:
        return (
            f"runtime gate denied: gate={decision.gate}; status={decision.status}; "
            f"findings={codes}; {model_message}{hint}"
        )
    return f"runtime gate denied: gate={decision.gate}; status={decision.status}; findings={codes}; 工具未授权或未通过运行时门{hint}"


def _has_path_finding(decision: GateDecision) -> bool:
    return any(code.startswith("PATH_") for code in decision.finding_codes)


__all__ = ["attach_runtime_gate", "runtime_gate_block_result"]
