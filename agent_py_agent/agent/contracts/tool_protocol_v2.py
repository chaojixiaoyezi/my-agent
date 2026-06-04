
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


def normalize_tool_call(payload: Any) -> ToolCallEnvelope:
    if isinstance(payload, ToolCallEnvelope):
        return payload
    data = _loads_if_json(payload)
    tool_name = str(data.get("tool_name") or data.get("tool") or "")
    raw_input = data.get("input")
    if raw_input is None:
        raw_input = data.get("args") or data.get("arguments")
    raw_status = str(data.get("status") or "").lower()
    status = raw_status if raw_status in STATUSES else "pending"
    if raw_input is None:
        raw_input = _flat_call_input(data, include_status=bool(raw_status and raw_status not in STATUSES))
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
    refs = _normalize_artifact_refs(data.get("artifact_refs") or data.get("artifacts") or [])
    metadata = data.get("metadata") or {}
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


def normalize_tool_result(payload: Any) -> ToolResultEnvelope:
    if isinstance(payload, ToolResultEnvelope):
        return payload
    data = _loads_if_json(payload)
    tool_name, operation_id = _result_identity(data)
    status = _status_from_result(data)
    idempotency_key = _result_idempotency_key(data, tool_name, operation_id, status)
    refs = _normalize_artifact_refs(data.get("artifact_refs") or data.get("artifacts") or [])
    metadata = data.get("metadata") or {}
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


def serialize_tool_call(envelope: ToolCallEnvelope | dict[str, Any]) -> str:
    return json.dumps(normalize_tool_call(envelope).to_dict(), ensure_ascii=False, sort_keys=True)


def serialize_tool_result(envelope: ToolResultEnvelope | dict[str, Any]) -> str:
    return json.dumps(normalize_tool_result(envelope).to_dict(), ensure_ascii=False, sort_keys=True)


def deserialize_tool_call(payload: str | bytes | dict[str, Any]) -> ToolCallEnvelope:
    return normalize_tool_call(payload)


def deserialize_tool_result(payload: str | bytes | dict[str, Any]) -> ToolResultEnvelope:
    return normalize_tool_result(payload)


def _flat_call_input(data: dict[str, Any], *, include_status: bool) -> dict[str, Any]:
    protocol_keys = {
        "args",
        "arguments",
        "artifact_refs",
        "artifacts",
        "call_id",
        "idempotency_key",
        "input",
        "kind",
        "metadata",
        "operation_id",
        "schema_version",
        "tool",
        "tool_name",
    }
    if not include_status:
        protocol_keys.add("status")
    return {key: value for key, value in data.items() if key not in protocol_keys}


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


def _result_identity(data: dict[str, Any]) -> tuple[str, str]:
    tool_name = str(data.get("tool_name") or data.get("tool") or "")
    call_id = str(data.get("call_id") or "")
    operation_id = str(data.get("operation_id") or "")
    if not operation_id and call_id:
        operation_id = f"tool_call:{call_id}"
    if not operation_id:
        operation_id = build_operation_id("tool_result", {"tool_name": tool_name, "output": data.get("output")})
    return tool_name, operation_id


def _result_idempotency_key(data: dict[str, Any], tool_name: str, operation_id: str, status: str) -> str:
    existing = str(data.get("idempotency_key") or "")
    if existing:
        return existing
    return build_idempotency_key(tool_name or "unknown_tool", {"operation_id": operation_id, "status": status})


def _result_error(data: dict[str, Any], status: str) -> ToolError | None:
    error_payload = data.get("error")
    if error_payload is None and status == "failed":
        error_payload = {
            "error_type": data.get("error_type", ""),
            "message": data.get("message") or data.get("detail") or "",
            "retry_hint": data.get("retry_hint", ""),
        }
    return ToolError.from_payload(error_payload) if error_payload is not None else None


def _result_output(data: dict[str, Any]) -> Any:
    if data.get("output") is None and "output_ref" in data:
        return {"output_ref": data.get("output_ref")}
    return data.get("output")


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


def _normalize_artifact_refs(payload: Any) -> list[ArtifactRef]:
    if not isinstance(payload, list):
        return []
    refs = [_artifact_ref_from_payload(item) for item in payload]
    return [item for item in refs if item.path]


def _artifact_ref_from_payload(payload: Any) -> ArtifactRef:
    if isinstance(payload, ArtifactRef):
        return payload
    if isinstance(payload, str):
        return ArtifactRef(artifact_id=payload, path=payload)
    if not isinstance(payload, dict):
        return ArtifactRef(artifact_id="", path="")
    path = str(payload.get("path") or payload.get("uri") or payload.get("ref") or "")
    size = payload.get("size_bytes")
    try:
        size_bytes = max(0, int(size or 0))
    except (TypeError, ValueError):
        size_bytes = 0
    mime_type = str(payload.get("mime_type") or payload.get("content_type") or "")
    return ArtifactRef(
        artifact_id=str(payload.get("artifact_id") or payload.get("id") or path),
        path=path,
        kind=str(payload.get("kind") or "generic"),
        owner_run_id=str(payload.get("owner_run_id") or ""),
        hash=str(payload.get("hash") or payload.get("digest") or ""),
        summary=str(payload.get("summary") or ""),
        size_bytes=size_bytes,
        mime_type=mime_type,
    )


def _artifact_ref_findings(refs: list[ArtifactRef]) -> list[str]:
    findings: list[str] = []
    for index, ref in enumerate(refs):
        if not ref.artifact_id:
            findings.append(f"artifact_refs[{index}].artifact_id_required")
        if not ref.path:
            findings.append(f"artifact_refs[{index}].path_required")
    return findings


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
