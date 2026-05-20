# LLM: Typed envelope helpers for ToolRegistry execution.
# 模块用途: 集中处理工具调用 envelope 的去重、执行前展开和执行后结果关联。

from __future__ import annotations

"""Tool registry bridge helpers for the typed action protocol."""

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


# LLM: legacy_payloads_to_tool_envelopes wraps parsed legacy payloads in typed action envelopes.
# 函数用途: 给旧文本解析结果去重并补 call_id/source/scope，供后续执行层按 envelope 追踪工具调用。
def legacy_payloads_to_tool_envelopes(
    payloads: list[dict[str, Any]],
    *,
    scope: RunScope | None = None,
    source: str = "legacy_text_protocol",
) -> list[ToolCallEnvelope]:
    return [
        tool_call_envelope_from_payload(
            ToolCallEnvelopePayloadRequest(
                payload=payload,
                call_id=f"legacy-tool-call-{index}",
                source=source,
                scope=scope,
                reserved={"legacy_index": index},
            )
        )
        for index, payload in enumerate(_dedupe_legacy_tool_payloads(payloads), start=1)
    ]


# LLM: tool_call_envelope_from_execution_payload accepts only typed executable action envelopes.
# 函数用途: 从执行 payload 中识别 ToolCallEnvelope；非 tool_call envelope 明确拒绝，普通 dict 继续走兼容路径。
def tool_call_envelope_from_execution_payload(
    payload: object,
) -> ToolCallEnvelope | ToolExecutionResult | None:
    if isinstance(payload, ToolCallEnvelope):
        return payload
    if not isinstance(payload, dict) or "kind" not in payload:
        return None
    if payload.get("kind") != "tool_call":
        return ToolExecutionResult("unknown", False, "expected tool_call envelope for execution")
    return ToolCallEnvelope.from_dict(payload)


# LLM: attach_result_envelope links execution output back to its typed call_id.
# 函数用途: 给 ToolExecutionResult 补 call_id 和 result_envelope；旧 dict 调用没有 envelope 时保持兼容。
def attach_result_envelope(
    result: ToolExecutionResult,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult:
    if envelope is None:
        return result
    result.call_id = envelope.call_id
    result.result_envelope = ToolCallResultEnvelope(
        call_id=envelope.call_id,
        tool=result.tool,
        ok=result.ok,
        output=result.output if not result.ok else "",
        error="" if result.ok else result.output,
        scope=envelope.scope,
        operation_id=envelope.operation_id,
        reserved={
            "source": envelope.source,
            "action_created_at": envelope.created_at,
        },
    ).to_dict()
    result.result_envelope["tool_protocol_v2"] = _tool_protocol_v2_payload(result, envelope)
    if not result.ok:
        result.result_envelope.update(_error_contract_payload(result))
    return result


# LLM: payload_from_tool_call_envelope restores the legacy flat payload for existing tools.
# 函数用途: 把 ToolCallEnvelope 的 tool/args 转回工具注册表当前可执行的扁平参数字典。
def payload_from_tool_call_envelope(envelope: ToolCallEnvelope) -> dict[str, Any]:
    return {
        "tool": envelope.tool,
        **dict(envelope.args),
    }


# LLM: _error_contract_payload mirrors ToolExecutionResult failure facts into typed envelopes.
# 函数用途: 让 action protocol 的工具失败结果也带 error_code/recovery_hint，供恢复和 UI 使用。
def _error_contract_payload(result: ToolExecutionResult) -> dict[str, object]:
    return {
        "error_code": result.error_code,
        "error_category": result.error_category,
        "retryable": result.retryable,
        "recommended_action": result.recommended_action,
        "recovery_hint": result.recovery_hint,
    }


# LLM: _tool_protocol_v2_payload mirrors registry results into the newer machine contract.
# 函数用途: 将现有 ToolExecutionResult + action envelope 转成 tool_protocol.v2 结构，供恢复/审计统一读取。
def _tool_protocol_v2_payload(result: ToolExecutionResult, envelope: ToolCallEnvelope) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_id": envelope.operation_id,
        "tool_name": result.tool,
        "idempotency_key": str(envelope.reserved.get("idempotency_key") or ""),
        "status": "succeeded" if result.ok else "failed",
        "output": "" if result.ok else result.output,
        "metadata": {
            "call_id": envelope.call_id,
            "source": envelope.source,
            "scope": envelope.scope.to_dict(),
        },
    }
    if not result.ok:
        payload["error"] = {
            "error_type": result.error_code,
            "message": result.output,
            "retry_hint": result.recommended_action,
            "retryable": result.retryable,
        }
    return normalize_tool_result(payload).to_dict()


# LLM: _dedupe_legacy_tool_payloads prevents XML-ish nested blocks from becoming duplicate actions.
# 函数用途: typed 迁移入口去重完全相同的旧 payload；不改变旧 parse_registry_tool_calls 的兼容行为。
def _dedupe_legacy_tool_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for payload in payloads:
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)
    return unique
