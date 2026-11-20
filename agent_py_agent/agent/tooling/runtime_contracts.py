# LLM: 调用和结果沿唯一执行器运输，宿主迁移事实与模型参数分离，调用ID和幂等身份不随规范化改变。
# 模块用途: 定义工具请求、执行结果与证据引用的共同数据合同。
from __future__ import annotations

"""Canonical contracts carried across provider, policy, execution and replay.

Only these values may authorize or prove a tool action.  Provider prose and
tool output text are untrusted data and cannot manufacture lifecycle fields.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..common import hot_json
from ..common.fast_dataclass import fast_frozen, fast_replace, slot_setter
from ..contracts.idempotency import operation_idempotency_key
from .input_schema import clone_json_value
from .versioned_input_decoder import canonical_error_contract as error_contract

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
_LIVE_PROMPT_PROJECTION_KEY = "live_prompt_projection"


@fast_frozen
@dataclass(frozen=True, slots=True)
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
        _set_choice_mode(self, mode)
        _set_choice_tool_name(self, name)
        _set_choice_reason(self, str(self.reason or "").strip())

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


_set_choice_mode = slot_setter(ToolChoice, "mode")
_set_choice_tool_name = slot_setter(ToolChoice, "tool_name")
_set_choice_reason = slot_setter(ToolChoice, "reason")


@fast_frozen
@dataclass(frozen=True, slots=True)
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
        _set_capability_provider(self, provider)
        _set_capability_endpoint(self, endpoint)
        _set_capability_model(self, model)
        _set_capability_evidence(self, evidence)
        _set_capability_observed_at(self, str(self.observed_at or "").strip())


_set_capability_provider = slot_setter(ProviderToolCapability, "provider")
_set_capability_endpoint = slot_setter(ProviderToolCapability, "endpoint")
_set_capability_model = slot_setter(ProviderToolCapability, "model")
_set_capability_evidence = slot_setter(ProviderToolCapability, "evidence")
_set_capability_observed_at = slot_setter(ProviderToolCapability, "observed_at")


@fast_frozen
@dataclass(frozen=True, slots=True)
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
        _set_protocol_snapshot_run_id(self, str(self.run_id or "").strip())
        _set_protocol_snapshot_source_protocol(self, protocol)


_set_protocol_snapshot_run_id = slot_setter(ToolProtocolSnapshot, "run_id")
_set_protocol_snapshot_source_protocol = slot_setter(ToolProtocolSnapshot, "source_protocol")


# LLM: 迁移事实来自未执行请求的宿主协议，不进入模型arguments，也不替代权限与幂等身份。
# 类用途: 运输规范调用及执行前已核的原始输入来源。
@fast_frozen
@dataclass(frozen=True, slots=True)
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
    input_migration: dict[str, Any] = field(default_factory=dict)

    # LLM: 身份与幂等沿原校验；迁移字典只复制宿主事实，不作为模型输入或权限。
    # 函数用途: 冻结规范调用与执行前已核的版本迁移信息。
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
        _set_call_call_id(self, call_id)
        _set_call_tool_name(self, tool_name)
        _set_call_arguments(self, clone_json_value(self.arguments))
        _set_call_source_protocol(self, protocol)
        _set_call_schema_hash(self, schema_hash)
        _set_call_run_id(self, run_id)
        _set_call_turn_id(self, turn_id)
        _set_call_attempt_id(self, attempt_id)
        _set_call_required_action_id(self, str(self.required_action_id or "").strip())
        _set_call_operation_id(self, operation_id)
        _set_call_idempotency_key(self, idempotency_key)
        _set_call_input_migration(self, clone_json_value(self.input_migration))

    @property
    def args_hash(self) -> str:
        return tool_arguments_hash(self.arguments)

    # LLM: 只有真实迁移才附信息，普通v2序列化逐字不增加字段，不携原始秘密参数。
    # 函数用途: 输出规范调用与可选旧输入的指纹事实。
    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": clone_json_value(self.arguments),
            "source_protocol": self.source_protocol,
            "schema_hash": self.schema_hash,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "attempt_id": self.attempt_id,
            "required_action_id": self.required_action_id,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "args_hash": self.args_hash,
            **({"input_migration": clone_json_value(self.input_migration)} if self.input_migration else {}),
        }


_set_call_call_id = slot_setter(ToolCall, "call_id")
_set_call_tool_name = slot_setter(ToolCall, "tool_name")
_set_call_arguments = slot_setter(ToolCall, "arguments")
_set_call_source_protocol = slot_setter(ToolCall, "source_protocol")
_set_call_schema_hash = slot_setter(ToolCall, "schema_hash")
_set_call_run_id = slot_setter(ToolCall, "run_id")
_set_call_turn_id = slot_setter(ToolCall, "turn_id")
_set_call_attempt_id = slot_setter(ToolCall, "attempt_id")
_set_call_required_action_id = slot_setter(ToolCall, "required_action_id")
_set_call_operation_id = slot_setter(ToolCall, "operation_id")
_set_call_idempotency_key = slot_setter(ToolCall, "idempotency_key")
_set_call_input_migration = slot_setter(ToolCall, "input_migration")


@fast_frozen
@dataclass(frozen=True, slots=True)
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
        _set_block_type(self, block_type)
        _set_block_text(self, str(self.text or ""))
        _set_block_data(self, clone_json_value(self.data))
        _set_block_ref(self, str(self.ref or "").strip())
        _set_block_mime_type(self, str(self.mime_type or "").strip())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.text:
            payload["text"] = self.text
        if self.data is not None:
            payload["data"] = clone_json_value(self.data)
        if self.ref:
            payload["ref"] = self.ref
        if self.mime_type:
            payload["mime_type"] = self.mime_type
        return payload


_set_block_type = slot_setter(ToolContentBlock, "type")
_set_block_text = slot_setter(ToolContentBlock, "text")
_set_block_data = slot_setter(ToolContentBlock, "data")
_set_block_ref = slot_setter(ToolContentBlock, "ref")
_set_block_mime_type = slot_setter(ToolContentBlock, "mime_type")


@fast_frozen
@dataclass(frozen=True, slots=True)
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
        _set_ref_kind(self, str(self.kind).strip())
        _set_ref_ref(self, str(self.ref).strip())
        _set_ref_sha256(self, str(self.sha256 or "").strip())
        _set_ref_size_bytes(self, max(0, int(self.size_bytes or 0)))
        _set_ref_summary(self, str(self.summary or "").strip())
        _set_ref_mime_type(self, str(self.mime_type or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "summary": self.summary,
            "mime_type": self.mime_type,
        }


_set_ref_kind = slot_setter(ToolResultRef, "kind")
_set_ref_ref = slot_setter(ToolResultRef, "ref")
_set_ref_sha256 = slot_setter(ToolResultRef, "sha256")
_set_ref_size_bytes = slot_setter(ToolResultRef, "size_bytes")
_set_ref_summary = slot_setter(ToolResultRef, "summary")
_set_ref_mime_type = slot_setter(ToolResultRef, "mime_type")


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


# LLM: This host-owned result is attached only after an exact ToolCall approval binding resumes.
# Handlers and provider prose must not synthesize it through generic metadata or output text.
# 类用途: 表示用户批准已经精确应用到同一次工具调用，供模型续轮和耐久审计读取。
@fast_frozen
@dataclass(frozen=True, slots=True)
class AppliedToolApproval:
    permission_id: str
    decision: str

    def __post_init__(self) -> None:
        permission_id = str(self.permission_id or "").strip()
        decision = str(self.decision or "").strip().lower()
        if not permission_id:
            raise ValueError("applied tool approval requires permission_id")
        if decision not in {"approved", "approved_session"}:
            raise ValueError(f"invalid applied tool approval decision: {decision}")
        _set_applied_approval_permission_id(self, permission_id)
        _set_applied_approval_decision(self, decision)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "tool_approval_result.v1",
            "permission_id": self.permission_id,
            "status": "approved",
            "decision": self.decision,
            "applied": True,
        }


_set_applied_approval_permission_id = slot_setter(AppliedToolApproval, "permission_id")
_set_applied_approval_decision = slot_setter(AppliedToolApproval, "decision")


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


@fast_frozen
@dataclass(frozen=True, slots=True)
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


# LLM: 结果是历史、结算和归档的唯一运输合同；可信输入迁移事实须与原调用同时进入结果，不复用为授权。
# 类用途: 保存工具执行的结构化结果、正文及来源，让归档和现场展示共用真实状态。
@fast_frozen
@dataclass(frozen=True, slots=True)
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
    applied_approval: AppliedToolApproval | None = None
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
        effect_outcome = str(self.effect_outcome or "").strip().lower()
        action_decision = self.metadata.get("action_decision") if isinstance(self.metadata, dict) else None
        resolved_effect = (
            str(action_decision.get("resolved_effect") or "").strip().lower()
            if isinstance(action_decision, dict)
            else ""
        )
        replayed_terminal_mutation = (
            self.operation is not None
            and bool(self.operation.replayed)
            and str(self.operation.status or "").strip().lower() in {"succeeded", "failed", "cancelled"}
            and resolved_effect not in {"", "read_only"}
        )
        code = str(self.error_code or "").strip().upper()
        category = str(self.error_category or "").strip()
        retryable = bool(self.retryable)
        recommended = str(self.recommended_action or "").strip()
        recovery = str(self.recovery_hint or "").strip()
        if status != "succeeded":
            contract = error_contract(code or "UNKNOWN_ERROR")
            code = contract.code
            category = contract.category
            retryable = (
                False
                if (effect_outcome == "unknown" or replayed_terminal_mutation)
                else contract.retryable
            )
            recommended = contract.recommended_action
            recovery = contract.recovery_hint
            if effect_outcome == "unknown" and recommended in {"retry", "retry_after_backoff"}:
                unknown_contract = error_contract("TOOL_OPERATION_OUTCOME_UNKNOWN")
                recommended = unknown_contract.recommended_action
                recovery = unknown_contract.recovery_hint
            elif replayed_terminal_mutation and recommended in {"retry", "retry_after_backoff"}:
                recommended = "change_strategy"
                recovery = "原副作用操作已处于终态重放，禁止原样重复提交同一写操作；请先核实当前目标状态后再换参或调整策略。"
        else:
            code = category = recommended = recovery = ""
            retryable = False
        trust = str(self.output_trust or "").strip().lower()
        redaction = str(self.output_redaction or "").strip().lower()
        if trust not in {"runtime", "external_data"}:
            raise ValueError(f"invalid tool output trust: {trust}")
        if redaction not in {"default", "source_code"}:
            raise ValueError(f"invalid tool output redaction: {redaction}")
        if self.applied_approval is not None and not isinstance(
            self.applied_approval,
            AppliedToolApproval,
        ):
            raise ValueError("tool result applied_approval must be typed")
        _set_result_call_id(self, str(self.call_id).strip())
        _set_result_tool_name(self, str(self.tool_name).strip())
        _set_result_status(self, status)
        _set_result_content_blocks(self, tuple(self.content_blocks))
        _set_result_error_code(self, code)
        _set_result_error_category(self, category)
        _set_result_retryable(self, retryable)
        _set_result_recommended_action(self, recommended)
        _set_result_recovery_hint(self, recovery)
        _set_result_failure_stage(self, failure_stage)
        _set_result_duration_ms(self, duration)
        _set_result_effect_outcome(self, effect_outcome)
        _set_result_effect_source_ref(self, str(self.effect_source_ref or "").strip())
        _set_result_refs(self, tuple(self.refs))
        _set_result_output_trust(self, trust)
        _set_result_output_redaction(self, redaction)
        _set_result_metadata(self, clone_json_value(self.metadata))

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
                parts.append(hot_json.dumps(block.data, ensure_ascii=False, sort_keys=True))
            elif block.type == "ref":
                parts.append(block.ref)
        return "\n".join(part for part in parts if part)

    @property
    def is_error(self) -> bool:
        return not self.ok

    # LLM: 迁移事实执行前已绑定call；归档和返回同源，不在执行后伪造持久化事实。
    # 函数用途: 构造带可信输入来源的成功结果。
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
            metadata={**facts.metadata, **({"input_migration": call.input_migration} if call.input_migration else {})},
        )

    # LLM: 拒绝与审批等待也保留宿主迁移事实，业务正文不可覆盖它。
    # 函数用途: 构造带原始输入指纹的失败结果。
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
            metadata={**facts.metadata, **({"input_migration": call.input_migration} if call.input_migration else {})},
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
        return fast_replace(self, **updates)

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
            "applied_approval": (
                self.applied_approval.to_dict() if self.applied_approval else None
            ),
            "effect_outcome": self.effect_outcome,
            "effect_source_ref": self.effect_source_ref,
            "refs": [ref.to_dict() for ref in self.refs],
            "output_trust": self.output_trust,
            "output_redaction": self.output_redaction,
            "metadata": clone_json_value(self.metadata),
        }

    def render_for_prompt(self, *, project_text: Callable[[str], str] | None = None) -> str:
        # 宿主组件分别脱敏后再拼接，不能将 JSON 正文退化成带协议前缀的普通文本重扫。
        project = project_text or (lambda value: value)
        fields = [
            f"tool={self.tool_name}",
            f"status={self.status}",
            f"handler_executed={'true' if self.handler_executed else 'false'}",
            f"duration_ms={self.duration_ms}",
        ]
        if self.error_code:
            host_error = self.metadata.get("host_error")
            host_code = host_error.get("code") if isinstance(host_error, dict) else None
            host_status = host_error.get("http_status") if isinstance(host_error, dict) else None
            trusted_host_error = (isinstance(host_error, dict) and host_error.get("source") == "business_host"
                and isinstance(host_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", host_code)
                and type(host_status) is int and 400 <= host_status <= 599)
            fields.extend(
                (
                    f"error_code={host_code if trusted_host_error else self.error_code}",
                    f"recommended_action={self.recommended_action}",
                )
            )
            if trusted_host_error:
                fields.extend((f"control_error_code={self.error_code}", f"http_status={host_status}"))
            # 领域原因和宿主恢复分类分开展示；只接收有界机器码，不能把任意回执正文拼进协议头。
            reported = self.metadata.get("reported_error_code")
            if (isinstance(reported, str) and reported != self.error_code
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", reported)):
                fields.append(f"reported_error_code={reported}")
        if self.failure_stage:
            fields.append(f"failure_stage={self.failure_stage}")
        if self.effect_outcome:
            fields.append(f"effect_outcome={self.effect_outcome}")
        from .models import source_effect_facts
        source_effect = source_effect_facts(self.metadata.get('source_effect'))
        if source_effect:
            fields.append(f"source_effect_outcome={source_effect['effect_outcome']}")
        parts = [project(f"[tool-result; {'; '.join(fields)}]"), project(self.output)]
        has_host_next_action = False
        if self.error_code and trusted_host_error and host_error.get("next_action"):
            from ..common.log_redaction import redact_sensitive_text

            action = host_error["next_action"]
            if isinstance(action, str) and action.strip():
                has_host_next_action = True
                parts.insert(1, "[host-business-recovery] " + project(hot_json.dumps({
                    "next_action": redact_sensitive_text(action[:2048])}, ensure_ascii=False)))
        raw_next_action = self.metadata.get("next_action") if isinstance(self.metadata, dict) else None
        if not has_host_next_action and isinstance(raw_next_action, str) and raw_next_action.strip():
            from ..common.log_redaction import redact_sensitive_text

            bounded_next = redact_sensitive_text(raw_next_action.strip()[:2048])
            if bounded_next:
                parts.append(
                    "[tool-next-action] "
                    + project(hot_json.dumps({"next_action": bounded_next}, ensure_ascii=False))
                )
        if self.applied_approval is not None:
            parts.append(
                "[tool-approval; status=approved; "
                f"decision={self.applied_approval.decision}; already_applied=true]"
            )
            parts.append(
                "The user already approved this exact tool call and the host already applied "
                "that approval before execution. Do not wait for or request another approval."
            )
        if self.recovery_hint:
            parts.append(
                "[tool-recovery; "
                f"retryable={'true' if self.retryable else 'false'}; "
                f"hint={project(self.recovery_hint)}]"
            )
        if self.refs:
            parts.append(
                "[tool-result-refs] "
                + project(hot_json.dumps([ref.to_dict() for ref in self.refs], ensure_ascii=False))
            )
        return "\n".join(part for part in parts if part)

    # LLM: Native provider history must use the same bounded projection as the text tool-context
    # path. Only the host-side loop may attach this metadata; raw handler output remains archived.
    # 函数用途: 给原生 tool_result 返回已经过外置、脱敏和恢复锚点处理的模型可见正文。
    def render_for_model_prompt(self) -> str:
        projected = self.metadata.get(_LIVE_PROMPT_PROJECTION_KEY)
        if isinstance(projected, str) and projected.strip():
            return projected
        return self.render_for_prompt()

    # LLM: Attach an ephemeral bounded model projection without changing canonical execution
    # facts, refs, status or the durable raw-output archive record.
    # 函数用途: 为当前原生工具历史绑定与文本链路完全一致的安全展示内容。
    def with_live_prompt_projection(self, projected: str) -> ToolResult:
        metadata = clone_json_value(self.metadata)
        metadata[_LIVE_PROMPT_PROJECTION_KEY] = str(projected or "")
        return fast_replace(self, metadata=metadata)

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


_set_result_call_id = slot_setter(ToolResult, "call_id")
_set_result_tool_name = slot_setter(ToolResult, "tool_name")
_set_result_status = slot_setter(ToolResult, "status")
_set_result_content_blocks = slot_setter(ToolResult, "content_blocks")
_set_result_error_code = slot_setter(ToolResult, "error_code")
_set_result_error_category = slot_setter(ToolResult, "error_category")
_set_result_retryable = slot_setter(ToolResult, "retryable")
_set_result_recommended_action = slot_setter(ToolResult, "recommended_action")
_set_result_recovery_hint = slot_setter(ToolResult, "recovery_hint")
_set_result_failure_stage = slot_setter(ToolResult, "failure_stage")
_set_result_duration_ms = slot_setter(ToolResult, "duration_ms")
_set_result_effect_outcome = slot_setter(ToolResult, "effect_outcome")
_set_result_effect_source_ref = slot_setter(ToolResult, "effect_source_ref")
_set_result_refs = slot_setter(ToolResult, "refs")
_set_result_output_trust = slot_setter(ToolResult, "output_trust")
_set_result_output_redaction = slot_setter(ToolResult, "output_redaction")
_set_result_metadata = slot_setter(ToolResult, "metadata")


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
