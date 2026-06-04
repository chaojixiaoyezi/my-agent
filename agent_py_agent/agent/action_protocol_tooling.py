
from __future__ import annotations

"""Tool protocol envelope layer."""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    RunScope,
    _default_operation_id,
    _dict_or_empty,
    _now_iso,
)


@dataclass(frozen=True)
class ToolCallEnvelopePayloadRequest:
    payload: dict[str, Any]
    call_id: str
    source: str
    scope: RunScope | None = None


@dataclass(frozen=True)
class ToolCallEnvelope:
    call_id: str
    source: str
    tool: str
    args: dict[str, Any]
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    idempotency_key: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "tool_call"
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.call_id))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolCallEnvelope:
        return cls(
            call_id=str(payload.get("call_id") or ""),
            source=str(payload.get("source") or ""),
            tool=str(payload.get("tool") or ""),
            args=_dict_or_empty(payload.get("args")),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "tool_call"),
            created_at=str(payload.get("created_at") or _now_iso()),
        )


@dataclass(frozen=True)
class ToolCallResultEnvelope:
    call_id: str
    tool: str
    ok: bool
    output_ref: str = ""
    output: str = ""
    error: str = ""
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    source: str = ""
    action_created_at: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "tool_call_result"
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.call_id))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolCallResultEnvelope:
        return cls(
            call_id=str(payload.get("call_id") or ""),
            tool=str(payload.get("tool") or ""),
            ok=bool(payload.get("ok")),
            output_ref=str(payload.get("output_ref") or ""),
            output=str(payload.get("output") or ""),
            error=str(payload.get("error") or ""),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            source=str(payload.get("source") or ""),
            action_created_at=str(payload.get("action_created_at") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "tool_call_result"),
            created_at=str(payload.get("created_at") or _now_iso()),
        )


def tool_call_envelope_from_payload(request: ToolCallEnvelopePayloadRequest) -> ToolCallEnvelope:
    payload = request.payload
    tool = str(payload.get("tool") or "").strip()
    args = {key: value for key, value in payload.items() if key not in {"tool", "idempotency_key"}}
    return ToolCallEnvelope(
        call_id=request.call_id,
        source=request.source,
        tool=tool,
        args=args,
        scope=request.scope or RunScope(),
        idempotency_key=str(payload.get("idempotency_key") or ""),
    )
