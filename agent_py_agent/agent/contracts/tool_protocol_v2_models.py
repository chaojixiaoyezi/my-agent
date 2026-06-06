
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..action_protocol_core import ArtifactRef
from .error_taxonomy import ERROR_CONTRACTS, error_contract

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
