
from __future__ import annotations

"""Tool registry bridge helpers for the typed action protocol."""

import hashlib
import json
from typing import Any

from ..action_protocol import (
    RunScope,
    ToolCallEnvelope,
    ToolCallEnvelopePayloadRequest,
    ToolCallResultEnvelope,
    tool_call_envelope_from_payload,
)
from ..contracts.tool_protocol_v2 import normalize_tool_result
from .models import ToolExecutionResult


def payloads_to_tool_envelopes(
    payloads: list[dict[str, Any]],
    *,
    scope: RunScope | None = None,
    source: str = "text_protocol",
) -> list[ToolCallEnvelope]:
    return [
        tool_call_envelope_from_payload(
            ToolCallEnvelopePayloadRequest(
                payload=payload,
                call_id=f"tool-call-{index}",
                source=source,
                scope=scope,
            )
        )
        for index, payload in enumerate(_dedupe_tool_payloads(payloads), start=1)
    ]


def tool_call_envelope_from_execution_payload(
    payload: object,
) -> ToolCallEnvelope | ToolExecutionResult | None:
    if isinstance(payload, ToolCallEnvelope):
        return payload
    if not isinstance(payload, dict):
        return None
    # Flat execution payloads are shaped as {"tool": name, **arguments}; a
    # legitimate tool argument may itself be named "kind".  Only the typed
    # outer envelope has no "tool" key and carries tool_name + input.
    if "tool" in payload:
        return None
    if "kind" in payload and payload.get("kind") != "tool_call":
        return ToolExecutionResult(
            "unknown",
            False,
            "expected tool_call envelope for execution",
            error_code="TOOL_CALL_PAYLOAD_INVALID",
        )
    if payload.get("kind") != "tool_call":
        return None
    if "tool_name" not in payload or "input" not in payload:
        return ToolExecutionResult(
            "unknown",
            False,
            "invalid tool_call envelope: tool_name and input are required",
            error_code="TOOL_CALL_PAYLOAD_INVALID",
        )
    return ToolCallEnvelope.from_dict(payload)


def attach_result_envelope(
    result: ToolExecutionResult,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult:
    if envelope is None:
        return result
    result.call_id = envelope.call_id
    existing = dict(result.result_envelope) if isinstance(result.result_envelope, dict) else {}
    protocol_payload = ToolCallResultEnvelope(
        call_id=envelope.call_id,
        tool=result.tool,
        ok=result.ok,
        output=result.output if not result.ok else "",
        error="" if result.ok else result.output,
        scope=envelope.scope,
        operation_id=envelope.operation_id,
        source=envelope.source,
        action_created_at=envelope.created_at,
    ).to_dict()
    result.result_envelope = {**existing, **protocol_payload}
    result.result_envelope["input_facts"] = tool_input_facts(envelope.input)
    result.result_envelope["tool_protocol_v2"] = _tool_protocol_v2_payload(result, envelope)
    if not result.ok:
        result.result_envelope.update(_error_contract_payload(result))
    attach_tool_execution_envelope(result)
    return result


def payload_from_tool_call_envelope(envelope: ToolCallEnvelope) -> dict[str, Any]:
    return {
        "tool": envelope.tool_name,
        **dict(envelope.input),
    }


def _error_contract_payload(result: ToolExecutionResult) -> dict[str, object]:
    return {
        "error_code": result.error_code,
        "reported_error_code": result.reported_error_code,
        "error_category": result.error_category,
        "retryable": result.retryable,
        "recommended_action": result.recommended_action,
        "recovery_hint": result.recovery_hint,
        "failure_stage": result.failure_stage,
        "handler_executed": result.handler_executed,
        "duration_ms": result.duration_ms,
    }


def _tool_protocol_v2_payload(result: ToolExecutionResult, envelope: ToolCallEnvelope) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_id": envelope.operation_id,
        "tool_name": result.tool,
        "idempotency_key": envelope.idempotency_key,
        "status": "succeeded" if result.ok else "failed",
        "output": "" if result.ok else result.output,
        "metadata": {
            "call_id": envelope.call_id,
            "source": envelope.source,
            "scope": envelope.scope.to_dict(),
            "tool_execution": _tool_execution_payload(result),
        },
    }
    if not result.ok:
        payload["error"] = {
            "error_type": result.error_code,
            "reported_type": result.reported_error_code,
            "message": result.output,
            "retry_hint": result.recommended_action,
            "retryable": result.retryable,
        }
    return normalize_tool_result(payload).to_dict()


# LLM: 失败层级、handler 进入事实和耗时只来自 host 执行边界；工具正文不能覆盖。
# 函数用途: 在结果被计时或副作用协调器改写后，同步唯一的 tool_execution 诊断块。
def attach_tool_execution_envelope(result: ToolExecutionResult) -> None:
    envelope = dict(result.result_envelope or {})
    execution = _tool_execution_payload(result)
    envelope["tool_execution"] = execution
    protocol = envelope.get("tool_protocol_v2")
    if isinstance(protocol, dict):
        metadata = protocol.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["tool_execution"] = execution
        protocol = dict(protocol)
        protocol["metadata"] = metadata
        envelope["tool_protocol_v2"] = protocol
    if not result.ok:
        envelope.update(_error_contract_payload(result))
    result.result_envelope = envelope


def _tool_execution_payload(result: ToolExecutionResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "handler_executed": bool(result.handler_executed),
        "duration_ms": max(0, int(result.duration_ms or 0)),
    }
    if result.failure_stage:
        payload["failure_stage"] = result.failure_stage
    return payload


# LLM: 审计和结果协议只保存字段形状与不可逆摘要，不复制命令、密钥或正文参数。
# 函数用途: 为一次工具输入生成可核对、可脱敏的稳定事实。
def tool_input_facts(value: object) -> dict[str, object]:
    payload = value if isinstance(value, dict) else {}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return {
        "field_names": sorted(str(key) for key in payload),
        "field_types": {str(key): type(payload[key]).__name__ for key in sorted(payload, key=str)},
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _dedupe_tool_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for payload in payloads:
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)
    return unique
