from __future__ import annotations

"""Canonical contracts carried across provider, policy, execution and replay.

Only these values may authorize or prove a tool action.  Provider prose and
tool output text are untrusted data and cannot manufacture lifecycle fields.
"""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ..contracts.error_taxonomy import error_contract
from ..contracts.idempotency import operation_idempotency_key

SourceProtocol = Literal["native", "text"]
ToolChoiceMode = Literal["auto", "required", "specific", "none"]
ToolResultStatus = Literal[
    "succeeded",
    "failed",
    "cancelled",
    "skipped",
    "approval_required",
    "unknown",
]

_TOOL_RESULT_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "skipped",
        "approval_required",
        "unknown",
    }
)
_SOURCE_PROTOCOLS = frozenset({"native", "text"})
_TOOL_CHOICE_MODES = frozenset({"auto", "required", "specific", "none"})


@dataclass(frozen=True)
class ToolChoice:
    """Provider-independent tool selection for one model turn."""

    mode: ToolChoiceMode = "auto"
    tool_name: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        mode = str(self.mode or "").strip().lower()
        name = str(self.tool_name or "").strip()
        if mode not in _TOOL_CHOICE_MODES:
            raise ValueError(f"invalid tool choice mode: {mode}")
        if mode == "specific" and not name:
            raise ValueError("specific tool choice requires tool_name")
        if mode != "specific" and name:
            raise ValueError(f"{mode} tool choice cannot carry tool_name")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "tool_name", name)
        object.__setattr__(self, "reason", str(self.reason or "").strip())

    @classmethod
    def auto(cls, reason: str = "") -> ToolChoice:
        return cls("auto", reason=reason)

    @classmethod
    def required(cls, reason: str = "") -> ToolChoice:
        return cls("required", reason=reason)

    @classmethod
    def specific(cls, tool_name: str, reason: str = "") -> ToolChoice:
        return cls("specific", tool_name=tool_name, reason=reason)

    @classmethod
    def none(cls, reason: str = "") -> ToolChoice:
        return cls("none", reason=reason)


@dataclass(frozen=True)
class ProviderToolCapability:
    """Observed provider/model/endpoint capability, not a model-name guess."""

    provider: str
    endpoint: str
    model: str
    stream: bool
    native_supported: bool
    evidence: str
    observed_at: str = ""

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip()
        endpoint = str(self.endpoint or "").strip()
        model = str(self.model or "").strip()
        evidence = str(self.evidence or "").strip()
        if not provider or not endpoint or not evidence:
            raise ValueError("provider tool capability requires provider, endpoint and evidence")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "observed_at", str(self.observed_at or "").strip())


@dataclass(frozen=True)
class ToolProtocolSnapshot:
    """Run-fixed protocol selection; it cannot change after the first request."""

    run_id: str
    source_protocol: SourceProtocol
    capability: ProviderToolCapability

    def __post_init__(self) -> None:
        protocol = str(self.source_protocol or "").strip().lower()
        if protocol not in _SOURCE_PROTOCOLS:
            raise ValueError(f"invalid tool source protocol: {protocol}")
        if protocol == "native" and not self.capability.native_supported:
            raise ValueError("native protocol selected without observed native capability")
        object.__setattr__(self, "run_id", str(self.run_id or "").strip())
        object.__setattr__(self, "source_protocol", protocol)


@dataclass(frozen=True)
class ToolCall:
    """The sole internal authority for one proposed tool invocation."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    source_protocol: SourceProtocol
    schema_hash: str
    run_id: str
    turn_id: str
    attempt_id: str
    required_action_id: str = ""
    operation_id: str = ""
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        call_id = str(self.call_id or "").strip()
        tool_name = str(self.tool_name or "").strip()
        protocol = str(self.source_protocol or "").strip().lower()
        schema_hash = str(self.schema_hash or "").strip()
        run_id = str(self.run_id or "").strip()
        turn_id = str(self.turn_id or "").strip()
        attempt_id = str(self.attempt_id or "").strip()
        if not call_id:
            raise ValueError("tool call_id is required")
        if not tool_name:
            raise ValueError("tool tool_name is required")
        if not isinstance(self.arguments, dict):
            raise ValueError("tool arguments must be an object")
        if protocol not in _SOURCE_PROTOCOLS:
            raise ValueError(f"invalid tool source protocol: {protocol}")
        if not schema_hash.startswith("sha256:"):
            raise ValueError("tool schema_hash is required")
        if not run_id or not turn_id or not attempt_id:
            raise ValueError("tool call requires run_id, turn_id and attempt_id")
        operation_id = str(self.operation_id or "").strip() or _default_operation_id(
            run_id,
            attempt_id,
            call_id,
        )
        idempotency_key = str(self.idempotency_key or "").strip() or operation_idempotency_key(
            run_id,
            operation_id,
        )
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "arguments", deepcopy(self.arguments))
        object.__setattr__(self, "source_protocol", protocol)
        object.__setattr__(self, "schema_hash", schema_hash)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "required_action_id", str(self.required_action_id or "").strip())
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)

    @property
    def args_hash(self) -> str:
        return tool_arguments_hash(self.arguments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": deepcopy(self.arguments),
            "source_protocol": self.source_protocol,
            "schema_hash": self.schema_hash,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "attempt_id": self.attempt_id,
            "required_action_id": self.required_action_id,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "args_hash": self.args_hash,
        }


@dataclass(frozen=True)
class ToolContentBlock:
    """One bounded model-facing result block or durable reference."""

    type: str
    text: str = ""
    data: Any = None
    ref: str = ""
    mime_type: str = ""

    def __post_init__(self) -> None:
        block_type = str(self.type or "").strip().lower()
        if block_type not in {"text", "json", "ref"}:
            raise ValueError(f"invalid tool content block type: {block_type}")
        if block_type == "ref" and not str(self.ref or "").strip():
            raise ValueError("ref content block requires ref")
        object.__setattr__(self, "type", block_type)
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "data", deepcopy(self.data))
        object.__setattr__(self, "ref", str(self.ref or "").strip())
        object.__setattr__(self, "mime_type", str(self.mime_type or "").strip())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.text:
            payload["text"] = self.text
        if self.data is not None:
            payload["data"] = deepcopy(self.data)
        if self.ref:
            payload["ref"] = self.ref
        if self.mime_type:
            payload["mime_type"] = self.mime_type
        return payload


@dataclass(frozen=True)
class ToolResultRef:
    kind: str
    ref: str
    sha256: str = ""
    size_bytes: int = 0
    summary: str = ""
    mime_type: str = ""

    def __post_init__(self) -> None:
        if not str(self.kind or "").strip() or not str(self.ref or "").strip():
            raise ValueError("tool result ref requires kind and ref")
        object.__setattr__(self, "kind", str(self.kind).strip())
        object.__setattr__(self, "ref", str(self.ref).strip())
        object.__setattr__(self, "sha256", str(self.sha256 or "").strip())
        object.__setattr__(self, "size_bytes", max(0, int(self.size_bytes or 0)))
        object.__setattr__(self, "summary", str(self.summary or "").strip())
        object.__setattr__(self, "mime_type", str(self.mime_type or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "summary": self.summary,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True)
class ToolOperation:
    operation_id: str
    idempotency_key: str
    args_hash: str
    status: str
    handler_executed: bool
    effect_outcome: str
    effect_source_ref: str = ""
    attempt_count: int = 1
    result_ref: str = ""
    created_at: str = ""
    updated_at: str = ""
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "args_hash": self.args_hash,
            "status": self.status,
            "handler_executed": self.handler_executed,
            "effect_outcome": self.effect_outcome,
            "effect_source_ref": self.effect_source_ref,
            "attempt_count": max(0, int(self.attempt_count or 0)),
            "result_ref": self.result_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class ToolSuccessFacts:
    """Optional host-owned facts attached to one successful canonical result."""

    content_blocks: tuple[ToolContentBlock, ...] = ()
    operation: ToolOperation | None = None
    effect_outcome: str = ""
    effect_source_ref: str = ""
    refs: tuple[ToolResultRef, ...] = ()
    output_trust: str = "runtime"
    output_redaction: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    handler_executed: bool = True
    duration_ms: int = 0


@dataclass(frozen=True)
class ToolFailureFacts:
    """Optional host-owned facts attached to one non-success canonical result."""

    handler_executed: bool = False
    duration_ms: int = 0
    operation: ToolOperation | None = None
    effect_outcome: str = "not_started"
    effect_source_ref: str = ""
    refs: tuple[ToolResultRef, ...] = ()
    output_trust: str = "runtime"
    output_redaction: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    status: ToolResultStatus = "failed"


@dataclass(frozen=True)
class ToolResult:
    """The sole call result carried to history, settlement and finalization."""

    call_id: str
    tool_name: str
    status: ToolResultStatus
    content_blocks: tuple[ToolContentBlock, ...] = ()
    error_code: str = ""
    error_category: str = ""
    retryable: bool = False
    recommended_action: str = ""
    recovery_hint: str = ""
    handler_executed: bool = False
    failure_stage: str = ""
    duration_ms: int = 0
    operation: ToolOperation | None = None
    effect_outcome: str = ""
    effect_source_ref: str = ""
    refs: tuple[ToolResultRef, ...] = ()
    output_trust: str = "runtime"
    output_redaction: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        status = str(self.status or "").strip().lower()
        if status not in _TOOL_RESULT_STATUSES:
            raise ValueError(f"invalid tool result status: {status}")
        if not str(self.call_id or "").strip() or not str(self.tool_name or "").strip():
            raise ValueError("tool result requires call_id and tool_name")
        duration = int(self.duration_ms or 0)
        if duration < 0:
            raise ValueError("tool result duration_ms must be non-negative")
        failure_stage = str(self.failure_stage or "").strip().lower()
        if status == "succeeded" and failure_stage:
            raise ValueError("successful tool result cannot carry failure_stage")
        code = str(self.error_code or "").strip().upper()
        category = str(self.error_category or "").strip()
        retryable = bool(self.retryable)
        recommended = str(self.recommended_action or "").strip()
        recovery = str(self.recovery_hint or "").strip()
        if status != "succeeded":
            contract = error_contract(code or "UNKNOWN_ERROR")
            code = contract.code
            category = contract.category
            retryable = contract.retryable
            recommended = contract.recommended_action
            recovery = contract.recovery_hint
        else:
            code = category = recommended = recovery = ""
            retryable = False
        trust = str(self.output_trust or "").strip().lower()
        redaction = str(self.output_redaction or "").strip().lower()
        if trust not in {"runtime", "external_data"}:
            raise ValueError(f"invalid tool output trust: {trust}")
        if redaction not in {"default", "source_code"}:
            raise ValueError(f"invalid tool output redaction: {redaction}")
        object.__setattr__(self, "call_id", str(self.call_id).strip())
        object.__setattr__(self, "tool_name", str(self.tool_name).strip())
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "content_blocks", tuple(self.content_blocks))
        object.__setattr__(self, "error_code", code)
        object.__setattr__(self, "error_category", category)
        object.__setattr__(self, "retryable", retryable)
        object.__setattr__(self, "recommended_action", recommended)
        object.__setattr__(self, "recovery_hint", recovery)
        object.__setattr__(self, "failure_stage", failure_stage)
        object.__setattr__(self, "duration_ms", duration)
        object.__setattr__(self, "effect_outcome", str(self.effect_outcome or "").strip().lower())
        object.__setattr__(self, "effect_source_ref", str(self.effect_source_ref or "").strip())
        object.__setattr__(self, "refs", tuple(self.refs))
        object.__setattr__(self, "output_trust", trust)
        object.__setattr__(self, "output_redaction", redaction)
        object.__setattr__(self, "metadata", deepcopy(self.metadata))

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @property
    def reported_error_code(self) -> str:
        return str(self.metadata.get("reported_error_code") or self.error_code)

    @property
    def output(self) -> str:
        parts: list[str] = []
        for block in self.content_blocks:
            if block.type == "text":
                parts.append(block.text)
            elif block.type == "json":
                parts.append(json.dumps(block.data, ensure_ascii=False, sort_keys=True))
            elif block.type == "ref":
                parts.append(block.ref)
        return "\n".join(part for part in parts if part)

    @property
    def is_error(self) -> bool:
        return not self.ok

    @classmethod
    def succeeded(
        cls,
        call: ToolCall,
        content: str = "",
        *,
        facts: ToolSuccessFacts | None = None,
    ) -> ToolResult:
        facts = facts or ToolSuccessFacts()
        blocks = facts.content_blocks or (
            (ToolContentBlock("text", text=content),) if content else ()
        )
        return cls(
            call_id=call.call_id,
            tool_name=call.tool_name,
            status="succeeded",
            content_blocks=blocks,
            handler_executed=facts.handler_executed,
            duration_ms=facts.duration_ms,
            operation=facts.operation,
            effect_outcome=facts.effect_outcome,
            effect_source_ref=facts.effect_source_ref,
            refs=facts.refs,
            output_trust=facts.output_trust,
            output_redaction=facts.output_redaction,
            metadata=facts.metadata,
        )

    @classmethod
    def failed(
        cls,
        call: ToolCall,
        content: str,
        *,
        error_code: str,
        failure_stage: str,
        facts: ToolFailureFacts | None = None,
    ) -> ToolResult:
        facts = facts or ToolFailureFacts()
        return cls(
            call_id=call.call_id,
            tool_name=call.tool_name,
            status=facts.status,
            content_blocks=(ToolContentBlock("text", text=content),) if content else (),
            error_code=error_code,
            handler_executed=facts.handler_executed,
            failure_stage=failure_stage,
            duration_ms=facts.duration_ms,
            operation=facts.operation,
            effect_outcome=facts.effect_outcome,
            effect_source_ref=facts.effect_source_ref,
            refs=facts.refs,
            output_trust=facts.output_trust,
            output_redaction=facts.output_redaction,
            metadata=facts.metadata,
        )

    def with_execution_facts(
        self,
        *,
        handler_executed: bool | None = None,
        failure_stage: str | None = None,
        duration_ms: int | float | None = None,
    ) -> ToolResult:
        updates: dict[str, Any] = {}
        if handler_executed is not None:
            updates["handler_executed"] = bool(handler_executed)
        if failure_stage is not None:
            updates["failure_stage"] = str(failure_stage or "").strip().lower()
        if duration_ms is not None:
            updates["duration_ms"] = max(0, int(float(duration_ms or 0)))
        return replace(self, **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "content_blocks": [block.to_dict() for block in self.content_blocks],
            "error_code": self.error_code,
            "error_category": self.error_category,
            "retryable": self.retryable,
            "recommended_action": self.recommended_action,
            "recovery_hint": self.recovery_hint,
            "handler_executed": self.handler_executed,
            "failure_stage": self.failure_stage,
            "duration_ms": self.duration_ms,
            "operation": self.operation.to_dict() if self.operation else None,
            "effect_outcome": self.effect_outcome,
            "effect_source_ref": self.effect_source_ref,
            "refs": [ref.to_dict() for ref in self.refs],
            "output_trust": self.output_trust,
            "output_redaction": self.output_redaction,
            "metadata": deepcopy(self.metadata),
        }

    def render_for_prompt(self) -> str:
        fields = [
            f"tool={self.tool_name}",
            f"status={self.status}",
            f"handler_executed={'true' if self.handler_executed else 'false'}",
            f"duration_ms={self.duration_ms}",
        ]
        if self.error_code:
            fields.extend(
                (
                    f"error_code={self.error_code}",
                    f"recommended_action={self.recommended_action}",
                )
            )
        if self.failure_stage:
            fields.append(f"failure_stage={self.failure_stage}")
        if self.effect_outcome:
            fields.append(f"effect_outcome={self.effect_outcome}")
        parts = [f"[tool-result; {'; '.join(fields)}]", self.output]
        if self.recovery_hint:
            parts.append(
                "[tool-recovery; "
                f"retryable={'true' if self.retryable else 'false'}; "
                f"hint={self.recovery_hint}]"
            )
        if self.refs:
            parts.append(
                "[tool-result-refs] "
                + json.dumps([ref.to_dict() for ref in self.refs], ensure_ascii=False)
            )
        return "\n".join(part for part in parts if part)

    def render_status_header(self) -> str:
        fields = [f"tool={self.tool_name}", f"status={self.status}"]
        if self.error_code:
            fields.extend(
                (
                    f"error_code={self.error_code}",
                    f"recommended_action={self.recommended_action}",
                )
            )
        if self.operation is not None:
            fields.extend(
                (
                    f"operation_id={self.operation.operation_id}",
                    f"operation_status={self.operation.status}",
                )
            )
        if self.effect_outcome:
            fields.append(f"effect_outcome={self.effect_outcome}")
        return f"[{'; '.join(fields)}]"

    def render_execution_facts(self) -> str:
        fields = [
            f"handler_executed={'true' if self.handler_executed else 'false'}",
            f"duration_ms={self.duration_ms}",
        ]
        if self.failure_stage:
            fields.insert(1, f"failure_stage={self.failure_stage}")
        return f"[tool-execution; {'; '.join(fields)}]"


def canonical_tool_call_from_persisted_payload(
    payload: object,
    *,
    runtime_snapshot: object,
    protocol_snapshot: object,
    run_id: str,
    turn_id: str,
    attempt_id: str,
    fallback_call_id: str,
) -> ToolCall:
    """Rehydrate typed carried/deferred state; never parse assistant prose."""

    if isinstance(payload, ToolCall):
        runtime = getattr(runtime_snapshot, "runtime", lambda _name: None)(payload.tool_name)
        if runtime is None:
            raise ValueError(f"carried tool call is outside runtime snapshot: {payload.tool_name}")
        if runtime.model_spec.schema_hash != payload.schema_hash:
            raise ValueError(f"carried tool call schema changed: {payload.tool_name}")
        return payload
    if not isinstance(payload, dict):
        raise ValueError("carried tool call payload must be an object")
    tool_name = str(payload.get("tool") or payload.get("tool_name") or "").strip()
    runtime = getattr(runtime_snapshot, "runtime", lambda _name: None)(tool_name)
    if runtime is None:
        raise ValueError(f"carried tool call is outside runtime snapshot: {tool_name}")
    arguments = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "tool",
            "tool_name",
            "call_id",
            "source_protocol",
            "schema_hash",
            "run_id",
            "turn_id",
            "attempt_id",
            "required_action_id",
            "operation_id",
            "idempotency_key",
        }
    }
    protocol = str(getattr(protocol_snapshot, "source_protocol", "") or "")
    return ToolCall(
        call_id=str(payload.get("call_id") or fallback_call_id or "").strip(),
        tool_name=tool_name,
        arguments=arguments,
        source_protocol=protocol,
        schema_hash=runtime.model_spec.schema_hash,
        run_id=str(run_id or "").strip(),
        turn_id=str(turn_id or "").strip(),
        attempt_id=str(attempt_id or "").strip(),
        required_action_id=str(payload.get("required_action_id") or "").strip(),
        operation_id=str(payload.get("operation_id") or "").strip(),
        idempotency_key=str(payload.get("idempotency_key") or "").strip(),
    )


def _default_operation_id(run_id: str, attempt_id: str, call_id: str) -> str:
    raw = f"{run_id}:{attempt_id}:{call_id}".encode()
    return "tool_operation:" + hashlib.sha256(raw).hexdigest()[:32]


def _json_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tool_arguments_hash(arguments: object) -> str:
    """Stable hash of the canonical argument object used by policy and ledgers."""

    return _json_hash(arguments)


__all__ = [
    "ProviderToolCapability",
    "SourceProtocol",
    "ToolCall",
    "ToolChoice",
    "ToolChoiceMode",
    "ToolContentBlock",
    "ToolFailureFacts",
    "ToolOperation",
    "ToolProtocolSnapshot",
    "ToolResult",
    "ToolResultRef",
    "ToolResultStatus",
    "ToolSuccessFacts",
    "canonical_tool_call_from_persisted_payload",
    "tool_arguments_hash",
]
