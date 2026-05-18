# LLM: Tool protocol v2 defines machine-readable tool call/result contracts without touching runtime flows.
# 模块用途: 提供工具调用、结果、错误和产物引用的结构化 envelope，以及 normalize/validate/JSON helpers。

from __future__ import annotations

import json
from typing import Any

from ..action_protocol_core import ArtifactRef
from .idempotency import idempotency_key as build_idempotency_key
from .idempotency import operation_id as build_operation_id
from .tool_protocol_v2_models import (
    SCHEMA_VERSION,
    STATUSES,
    OperationRef,
    ToolCallEnvelope,
    ToolError,
    ToolResultEnvelope,
    ToolResultFailureParams,
    json_stable,
)


# LLM: normalize_tool_call converts v2 and legacy tool-call dicts into ToolCallEnvelope.
# 函数用途: 兼容 tool/args/call_id 等旧字段，生成稳定 operation_id 和 idempotency_key。
def normalize_tool_call(payload: Any) -> ToolCallEnvelope:
    if isinstance(payload, ToolCallEnvelope):
        return payload
    data = _loads_if_json(payload)
    tool_name = str(data.get("tool_name") or data.get("tool") or "")
    raw_input = data.get("input")
    if raw_input is None:
        raw_input = data.get("args") or data.get("arguments") or {}
    input_payload = json_stable(raw_input if isinstance(raw_input, dict) else {"value": raw_input})
    call_id = str(data.get("call_id") or "")
    operation_id = str(data.get("operation_id") or "")
    if not operation_id and call_id:
        operation_id = f"tool_call:{call_id}"
    if not operation_id:
        operation_id = build_operation_id("tool_call", {"tool_name": tool_name, "input": input_payload})
    idempotency_key = str(data.get("idempotency_key") or "")
    if not idempotency_key:
        idempotency_key = build_idempotency_key(tool_name or "unknown_tool", input_payload)
    status = str(data.get("status") or "pending").lower()
    refs = _normalize_artifact_refs(data.get("artifact_refs") or data.get("artifacts") or [])
    metadata = data.get("metadata") or data.get("reserved") or {}
    return ToolCallEnvelope(
        operation_id=operation_id,
        tool_name=tool_name,
        input=input_payload,
        idempotency_key=idempotency_key,
        schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        status=status,
        artifact_refs=refs,
        metadata=json_stable(metadata) if isinstance(metadata, dict) else {"value": str(metadata)},
    )


# LLM: normalize_tool_result converts v2 and legacy result dicts into ToolResultEnvelope.
# 函数用途: 兼容 ok/output_ref/tool/call_id 等旧字段，并把错误统一分类为 ToolError。
def normalize_tool_result(payload: Any) -> ToolResultEnvelope:
    if isinstance(payload, ToolResultEnvelope):
        return payload
    data = _loads_if_json(payload)
    tool_name, operation_id = _result_identity(data)
    status = _status_from_result(data)
    idempotency_key = _result_idempotency_key(data, tool_name, operation_id, status)
    refs = _normalize_artifact_refs(data.get("artifact_refs") or data.get("artifacts") or [])
    metadata = data.get("metadata") or data.get("reserved") or {}
    return ToolResultEnvelope(
        operation_id=operation_id,
        tool_name=tool_name,
        status=status,
        output=json_stable(_result_output(data)),
        error=_result_error(data, status),
        artifact_refs=refs,
        idempotency_key=idempotency_key,
        schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        operation_ref=_result_operation_ref(data, tool_name, operation_id, idempotency_key),
        metadata=json_stable(metadata) if isinstance(metadata, dict) else {"value": str(metadata)},
    )


# LLM: validate_tool_call returns machine-checkable contract violations for a call envelope.
# 函数用途: 校验必填字段、schema_version、status 和 artifact refs，不抛异常打断调用方。
def validate_tool_call(envelope: ToolCallEnvelope | dict[str, Any]) -> list[str]:
    call = normalize_tool_call(envelope)
    findings: list[str] = []
    if call.schema_version != SCHEMA_VERSION:
        findings.append("schema_version_mismatch")
    if not call.operation_id:
        findings.append("operation_id_required")
    if not call.tool_name:
        findings.append("tool_name_required")
    if not call.idempotency_key:
        findings.append("idempotency_key_required")
    if call.status not in STATUSES:
        findings.append("status_invalid")
    findings.extend(_artifact_ref_findings(call.artifact_refs))
    return findings


# LLM: validate_tool_result returns machine-checkable contract violations for a result envelope.
# 函数用途: 校验结果状态、错误字段和 refs 合同，失败结果必须携带结构化 ToolError。
def validate_tool_result(envelope: ToolResultEnvelope | dict[str, Any]) -> list[str]:
    result = normalize_tool_result(envelope)
    findings: list[str] = []
    if result.schema_version != SCHEMA_VERSION:
        findings.append("schema_version_mismatch")
    if not result.operation_id:
        findings.append("operation_id_required")
    if not result.tool_name:
        findings.append("tool_name_required")
    if not result.idempotency_key:
        findings.append("idempotency_key_required")
    if result.status not in STATUSES:
        findings.append("status_invalid")
    if result.status == "failed" and result.error is None:
        findings.append("error_required_for_failed_result")
    findings.extend(_artifact_ref_findings(result.artifact_refs))
    return findings


# LLM: serialize_tool_call writes a normalized call envelope as deterministic JSON.
# 函数用途: 给日志、测试和跨进程边界提供稳定 JSON 字符串。
def serialize_tool_call(envelope: ToolCallEnvelope | dict[str, Any]) -> str:
    return json.dumps(normalize_tool_call(envelope).to_dict(), ensure_ascii=False, sort_keys=True)


# LLM: serialize_tool_result writes a normalized result envelope as deterministic JSON.
# 函数用途: 给日志、测试和跨进程边界提供稳定 JSON 字符串。
def serialize_tool_result(envelope: ToolResultEnvelope | dict[str, Any]) -> str:
    return json.dumps(normalize_tool_result(envelope).to_dict(), ensure_ascii=False, sort_keys=True)


# LLM: deserialize_tool_call reads JSON/dict payloads into a ToolCallEnvelope.
# 函数用途: 作为工具协议 v2 的读取边界，兼容旧字段并返回规范对象。
def deserialize_tool_call(payload: str | bytes | dict[str, Any]) -> ToolCallEnvelope:
    return normalize_tool_call(payload)


# LLM: deserialize_tool_result reads JSON/dict payloads into a ToolResultEnvelope.
# 函数用途: 作为工具协议 v2 的读取边界，兼容旧字段并返回规范对象。
def deserialize_tool_result(payload: str | bytes | dict[str, Any]) -> ToolResultEnvelope:
    return normalize_tool_result(payload)


# LLM: _loads_if_json accepts protocol payloads as JSON strings or dict-like data.
# 函数用途: 为 deserialize/normalize 统一输入边界，非对象 JSON 降成空对象。
def _loads_if_json(payload: Any) -> dict[str, Any]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return payload if isinstance(payload, dict) else {}


# LLM: _result_identity keeps normalize_tool_result below the function-size guard.
# 函数用途: 从结果 payload 中恢复 tool_name 和 operation_id，兼容 call_id 旧字段。
def _result_identity(data: dict[str, Any]) -> tuple[str, str]:
    tool_name = str(data.get("tool_name") or data.get("tool") or "")
    call_id = str(data.get("call_id") or "")
    operation_id = str(data.get("operation_id") or "")
    if not operation_id and call_id:
        operation_id = f"tool_call:{call_id}"
    if not operation_id:
        operation_id = build_operation_id("tool_result", {"tool_name": tool_name, "output": data.get("output")})
    return tool_name, operation_id


# LLM: _result_idempotency_key derives replay safety from structured result fields.
# 函数用途: 读取或生成工具结果幂等键，不依赖结果自然语言。
def _result_idempotency_key(data: dict[str, Any], tool_name: str, operation_id: str, status: str) -> str:
    existing = str(data.get("idempotency_key") or "")
    if existing:
        return existing
    return build_idempotency_key(tool_name or "unknown_tool", {"operation_id": operation_id, "status": status})


# LLM: _result_error builds ToolError only when the structured status is failed.
# 函数用途: 归一化失败结果的错误字段，成功结果不制造错误对象。
def _result_error(data: dict[str, Any], status: str) -> ToolError | None:
    error_payload = data.get("error")
    if error_payload is None and status == "failed":
        error_payload = {
            "error_type": data.get("error_type", ""),
            "message": data.get("message") or data.get("detail") or "",
            "retry_hint": data.get("retry_hint", ""),
        }
    return ToolError.from_payload(error_payload) if error_payload is not None else None


# LLM: _result_output keeps output refs structured without scanning prose for paths.
# 函数用途: 兼容 legacy output_ref；否则返回 output 原值。
def _result_output(data: dict[str, Any]) -> Any:
    if data.get("output") is None and "output_ref" in data:
        return {"output_ref": data.get("output_ref")}
    return data.get("output")


# LLM: _result_operation_ref keeps result envelopes linked to their call operation.
# 函数用途: 从 operation_ref 字段恢复引用；缺失时用结构化 result 身份补齐。
def _result_operation_ref(
    data: dict[str, Any],
    tool_name: str,
    operation_id: str,
    idempotency_key: str,
) -> OperationRef:
    operation_ref = OperationRef.from_payload(data.get("operation_ref") or {})
    if operation_ref.operation_id:
        return operation_ref
    return OperationRef(
        operation_id=operation_id,
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
    )


# LLM: _normalize_artifact_refs preserves only explicit artifact refs from structured fields.
# 函数用途: 规范化 artifact_refs 列表；不会读取 output/summary 等自然语言来造 refs。
def _normalize_artifact_refs(payload: Any) -> list[ArtifactRef]:
    if not isinstance(payload, list):
        return []
    refs = [_artifact_ref_from_payload(item) for item in payload]
    return [item for item in refs if item.path]


# LLM: _artifact_ref_from_payload converts explicit refs into the canonical action-protocol ArtifactRef.
# 函数用途: 只从 artifact_refs 结构字段恢复产物引用；不会扫描 output/summary 等自然语言。
def _artifact_ref_from_payload(payload: Any) -> ArtifactRef:
    if isinstance(payload, ArtifactRef):
        return payload
    if isinstance(payload, str):
        return ArtifactRef(artifact_id=payload, path=payload)
    if not isinstance(payload, dict):
        return ArtifactRef(artifact_id="", path="")
    path = str(payload.get("path") or payload.get("uri") or payload.get("ref") or "")
    metadata = payload.get("metadata") or payload.get("reserved") or {}
    reserved = json_stable(metadata) if isinstance(metadata, dict) else {"metadata": str(metadata)}
    size = payload.get("size_bytes")
    if size is not None:
        try:
            reserved = {**reserved, "size_bytes": int(size)}
        except (TypeError, ValueError):
            reserved = {**reserved, "size_bytes": str(size)}
    mime_type = str(payload.get("mime_type") or payload.get("content_type") or "")
    if mime_type:
        reserved = {**reserved, "mime_type": mime_type}
    return ArtifactRef(
        artifact_id=str(payload.get("artifact_id") or payload.get("id") or path),
        path=path,
        kind=str(payload.get("kind") or "generic"),
        owner_run_id=str(payload.get("owner_run_id") or ""),
        hash=str(payload.get("hash") or payload.get("digest") or ""),
        summary=str(payload.get("summary") or ""),
        reserved=reserved,
    )


# LLM: _artifact_ref_findings validates artifact refs without checking filesystem state.
# 函数用途: 返回 refs 合同问题，保持该模块纯数据校验、不做 I/O。
def _artifact_ref_findings(refs: list[ArtifactRef]) -> list[str]:
    findings: list[str] = []
    for index, ref in enumerate(refs):
        if not ref.artifact_id:
            findings.append(f"artifact_refs[{index}].artifact_id_required")
        if not ref.path:
            findings.append(f"artifact_refs[{index}].path_required")
    return findings


# LLM: _status_from_result maps legacy ok booleans and v2 status strings into one status vocabulary.
# 函数用途: 兼容旧结果字段，同时让失败结果进入结构化错误校验链路。
def _status_from_result(data: dict[str, Any]) -> str:
    if data.get("status"):
        return str(data["status"]).lower()
    if "ok" in data:
        return "succeeded" if bool(data["ok"]) else "failed"
    return "succeeded" if data.get("error") is None else "failed"


__all__ = [
    "SCHEMA_VERSION",
    "ArtifactRef",
    "OperationRef",
    "ToolCallEnvelope",
    "ToolError",
    "ToolResultEnvelope",
    "deserialize_tool_call",
    "deserialize_tool_result",
    "normalize_tool_call",
    "normalize_tool_result",
    "serialize_tool_call",
    "serialize_tool_result",
    "validate_tool_call",
    "validate_tool_result",
]
