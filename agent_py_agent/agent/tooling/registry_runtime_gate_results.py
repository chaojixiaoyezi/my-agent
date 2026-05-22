# LLM: Registry runtime-gate result helpers keep denial envelopes machine-readable.
# 模块用途: 把运行时 gate 决策挂到 ToolExecutionResult，供 closeout、replay 和审计读取。

from __future__ import annotations

from typing import Any

from ..action_protocol import ToolCallEnvelope
from ..contracts.gates import GateDecision
from .models import ToolExecutionResult
from .registry_envelopes import attach_result_envelope


# LLM: runtime_gate_block_result turns mandatory gate denials into normal tool results.
# 函数用途: 工具执行前 gate 拒绝时，返回统一 ToolExecutionResult 并把 gate 决策写进 result_envelope。
def runtime_gate_block_result(
    payload: dict[str, Any],
    decision: GateDecision,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult:
    result = ToolExecutionResult(
        str(decision.evidence.get("tool_name") or payload.get("tool") or "unknown"),
        False,
        _gate_output(decision),
    )
    result = attach_result_envelope(result, envelope)
    attach_runtime_gate(result, decision)
    return result


# LLM: attach_runtime_gate preserves gate facts for replay and acceptance without changing prompt output shape.
# 函数用途: 把运行时 gate 决策挂到工具结果 envelope；旧调用没有 envelope 时也保留 runtime_gate 字段。
def attach_runtime_gate(result: ToolExecutionResult, decision: GateDecision) -> None:
    envelope = dict(result.result_envelope or {})
    envelope["runtime_gate"] = decision.to_dict()
    result.result_envelope = envelope


# LLM: _gate_output renders a compact denial message while machine facts stay in result_envelope.
# 函数用途: 给旧 prompt 输出保留可读错误，真正判断仍读取 runtime_gate 结构字段。
def _gate_output(decision: GateDecision) -> str:
    codes = ",".join(decision.finding_codes) or "RUNTIME_GATE_DENIED"
    hint = " 路径超出允许的工作区范围，请使用工作区内或已授权 root 下的路径。" if _has_path_finding(decision) else ""
    return f"runtime gate denied: gate={decision.gate}; status={decision.status}; findings={codes}; 工具未授权或未通过运行时门{hint}"


# LLM: _has_path_finding keeps legacy path diagnostics visible while machine facts stay in runtime_gate.
# 函数用途: path gate 拒绝时补充旧调用方断言依赖的人类可读路径提示。
def _has_path_finding(decision: GateDecision) -> bool:
    return any(code.startswith("PATH_") for code in decision.finding_codes)


__all__ = ["attach_runtime_gate", "runtime_gate_block_result"]
