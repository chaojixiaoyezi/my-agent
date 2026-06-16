"""工具调用协议 v2：类型定义与归一化/校验逻辑。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..action_protocol import ArtifactRef
from .error_taxonomy import ERROR_CONTRACTS, error_contract
from .idempotency import idempotency_key as build_idempotency_key
from .idempotency import operation_id as build_operation_id
from .protocol_status import TOOL_STATUS_FAILED

# ---- 协议数据类型（原 tool_protocol_v2_models.py 并入）----
SCHEMA_VERSION = "tool_protocol.v2"
STATUSES = {"pending", "running", "succeeded", "failed", "cancelled", "skipped"}


def json_stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_stable(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [json_stable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class OperationRef:
    operation_id: str
    tool_name: str
    idempotency_key: str = ""
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, str]:
        return {
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> OperationRef:
        if isinstance(payload, OperationRef):
            return payload
        data = payload if isinstance(payload, dict) else {}
        return cls(
            operation_id=str(data.get("operation_id", "")),
            tool_name=str(data.get("tool_name") or ""),
            idempotency_key=str(data.get("idempotency_key", "")),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class ToolError:
    error_type: str
    message: str = ""
    retry_hint: str = ""
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "retry_hint": self.retry_hint,
            "retryable": self.retryable,
            "details": json_stable(self.details),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ToolError:
        if isinstance(payload, ToolError):
            return payload
        data = payload if isinstance(payload, dict) else {"message": str(payload or "")}
        message = str(data.get("message") or "")
        explicit_type = str(data.get("error_type") or "").upper()
        contract = _error_contract_for(explicit_type, message)
        details = data.get("details") or {}
        return cls(
            error_type=contract.code,
            message=message,
            retry_hint=str(data.get("retry_hint") or contract.recommended_action),
            retryable=bool(data.get("retryable", contract.retryable)),
            details=json_stable(details) if isinstance(details, dict) else {"value": str(details)},
        )


@dataclass(frozen=True)
class ToolCallEnvelope:
    operation_id: str
    tool_name: str
    input: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    schema_version: str = SCHEMA_VERSION
    status: str = "pending"
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "input": json_stable(self.input),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs if item.path],
            "metadata": json_stable(self.metadata),
        }

    def operation_ref(self) -> OperationRef:
        return OperationRef(
            operation_id=self.operation_id,
            tool_name=self.tool_name,
            idempotency_key=self.idempotency_key,
            schema_version=self.schema_version,
        )


@dataclass(frozen=True)
class ToolResultFailureParams:
    call: ToolCallEnvelope
    error: Any
    output: Any = None
    artifact_refs: list[ArtifactRef] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolResultEnvelope:
    operation_id: str
    tool_name: str
    status: str
    output: Any = None
    error: ToolError | None = None
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    idempotency_key: str = ""
    schema_version: str = SCHEMA_VERSION
    operation_ref: OperationRef | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        call: ToolCallEnvelope,
        *,
        output: Any = None,
        artifact_refs: list[ArtifactRef] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResultEnvelope:
        refs = artifact_refs if artifact_refs is not None else call.artifact_refs
        return cls(
            operation_id=call.operation_id,
            tool_name=call.tool_name,
            status="succeeded",
            output=json_stable(output),
            artifact_refs=refs,
            idempotency_key=call.idempotency_key,
            schema_version=call.schema_version,
            operation_ref=call.operation_ref(),
            metadata=metadata or {},
        )

    @classmethod
    def failure(cls, params: ToolResultFailureParams) -> ToolResultEnvelope:
        refs = params.artifact_refs if params.artifact_refs is not None else params.call.artifact_refs
        return cls(
            operation_id=params.call.operation_id,
            tool_name=params.call.tool_name,
            status="failed",
            output=json_stable(params.output),
            error=ToolError.from_payload(params.error),
            artifact_refs=refs,
            idempotency_key=params.call.idempotency_key,
            schema_version=params.call.schema_version,
            operation_ref=params.call.operation_ref(),
            metadata=params.metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        operation_ref = self.operation_ref or OperationRef(
            operation_id=self.operation_id,
            tool_name=self.tool_name,
            idempotency_key=self.idempotency_key,
            schema_version=self.schema_version,
        )
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "output": json_stable(self.output),
            "error": self.error.to_dict() if self.error else None,
            "retry_hint": self.error.retry_hint if self.error else "",
            "error_type": self.error.error_type if self.error else "",
            "artifact_refs": [item.to_dict() for item in self.artifact_refs if item.path],
            "operation_ref": operation_ref.to_dict(),
            "metadata": json_stable(self.metadata),
        }


def _error_contract_for(explicit_type: str, message: str):
    if explicit_type and explicit_type in ERROR_CONTRACTS:
        return error_contract(explicit_type)
    return error_contract("UNKNOWN_ERROR")


__all__ = [
    "SCHEMA_VERSION",
    "STATUSES",
    "ArtifactRef",
    "OperationRef",
    "ToolCallEnvelope",
    "ToolError",
    "ToolResultEnvelope",
    "ToolResultFailureParams",
    "json_stable",
]


# ---- 协议逻辑 ----
def normalize_tool_call(payload: Any) -> ToolCallEnvelope:
    if isinstance(payload, ToolCallEnvelope):
        return payload
    data = _loads_if_json(payload)
    tool_name = str(data.get("tool_name") or "")
    raw_input = data.get("input")
    raw_status = str(data.get("status") or "")
    status = _call_status(data, raw_status, raw_input)
    if raw_input is None:
        raw_input = {}
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
    refs = _normalize_artifact_refs(data.get("artifact_refs") or [])
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
    refs = _normalize_artifact_refs(data.get("artifact_refs") or [])
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
    if result.status == TOOL_STATUS_FAILED and result.error is None:
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


def _call_status(data: dict[str, Any], raw_status: str, raw_input: Any) -> str:
    if not raw_status:
        return "pending"
    if raw_status in STATUSES:
        return raw_status
    if raw_input is None and not _looks_like_protocol_envelope(data):
        return "pending"
    return raw_status


def _looks_like_protocol_envelope(data: dict[str, Any]) -> bool:
    return any(
        key in data
        for key in (
            "schema_version",
            "operation_id",
            "idempotency_key",
            "input",
            "artifact_refs",
            "metadata",
        )
    )


def _loads_if_json(payload: Any) -> dict[str, Any]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return payload if isinstance(payload, dict) else {}


def _result_identity(data: dict[str, Any]) -> tuple[str, str]:
    tool_name = str(data.get("tool_name") or "")
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
    if error_payload is None and status == TOOL_STATUS_FAILED:
        error_payload = {
            "error_type": data.get("error_type", ""),
            "message": data.get("message") or "",
            "retry_hint": data.get("retry_hint", ""),
        }
    return ToolError.from_payload(error_payload) if error_payload is not None else None


def _result_output(data: dict[str, Any]) -> Any:
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
    path = str(payload.get("path") or "")
    size = payload.get("size_bytes")
    try:
        size_bytes = max(0, int(size or 0))
    except (TypeError, ValueError):
        size_bytes = 0
    mime_type = str(payload.get("mime_type") or "")
    return ArtifactRef(
        artifact_id=str(payload.get("artifact_id") or ""),
        path=path,
        kind=str(payload.get("kind") or "generic"),
        owner_run_id=str(payload.get("owner_run_id") or ""),
        hash=str(payload.get("hash") or ""),
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
        return str(data["status"])
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
