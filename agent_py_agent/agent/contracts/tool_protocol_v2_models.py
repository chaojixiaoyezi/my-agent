# LLM: Tool protocol v2 models stay separate from codec helpers to keep contract files small.
# 模块用途: 定义工具协议 v2 的 envelope、错误和 operation 引用数据模型。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..action_protocol_core import ArtifactRef
from .error_taxonomy import ERROR_CONTRACTS, classify_error, error_contract

SCHEMA_VERSION = "tool_protocol.v2"
STATUSES = {"pending", "running", "succeeded", "failed", "cancelled", "skipped"}


# LLM: json_stable normalizes values so protocol hashes and payloads never depend on dict ordering.
# 函数用途: 把任意输入规整成 JSON 稳定结构，避免从自然语言摘要里猜机器字段。
def json_stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_stable(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [json_stable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# LLM: OperationRef is the compact cross-envelope pointer for replay, recovery, and idempotency checks.
# 类用途: 保存 operation_id、tool_name、schema_version 和 idempotency_key，供结果引用调用来源。
@dataclass(frozen=True)
class OperationRef:
    operation_id: str
    tool_name: str
    idempotency_key: str = ""
    schema_version: str = SCHEMA_VERSION

    # LLM: to_dict serializes OperationRef as a plain JSON object.
    # 函数用途: 输出稳定字段名，方便 envelope 嵌套和日志记录。
    def to_dict(self) -> dict[str, str]:
        return {
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
        }

    # LLM: from_payload accepts legacy or v2 operation pointers and returns one normalized ref.
    # 函数用途: 把 dict/OperationRef 转为 OperationRef；缺失字段由外层 envelope 补齐。
    @classmethod
    def from_payload(cls, payload: Any) -> OperationRef:
        if isinstance(payload, OperationRef):
            return payload
        data = payload if isinstance(payload, dict) else {}
        return cls(
            operation_id=str(data.get("operation_id", "")),
            tool_name=str(data.get("tool_name") or data.get("tool") or ""),
            idempotency_key=str(data.get("idempotency_key", "")),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        )


# LLM: ToolError maps raw failures into stable taxonomy fields for callers and retry logic.
# 类用途: 保存 error_type、message、retry_hint、retryable 和 details，未知错误降级为 UNKNOWN_ERROR。
@dataclass(frozen=True)
class ToolError:
    error_type: str
    message: str = ""
    retry_hint: str = ""
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes ToolError for envelopes and JSON logs.
    # 函数用途: 输出稳定错误字段，避免调用方从异常文本里二次猜分类。
    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "retry_hint": self.retry_hint,
            "retryable": self.retryable,
            "details": json_stable(self.details),
        }

    # LLM: from_payload classifies dict/string/exception-like errors into ToolError.
    # 函数用途: 优先信任已知结构化 error_type；未知或缺失时降级分类到 UNKNOWN_ERROR。
    @classmethod
    def from_payload(cls, payload: Any) -> ToolError:
        if isinstance(payload, ToolError):
            return payload
        data = payload if isinstance(payload, dict) else {"message": str(payload or "")}
        message = str(data.get("message") or data.get("error") or data.get("detail") or "")
        explicit_type = str(data.get("error_type") or data.get("code") or "").upper()
        contract = _error_contract_for(explicit_type, message)
        details = data.get("details") or {}
        return cls(
            error_type=contract.code,
            message=message,
            retry_hint=str(data.get("retry_hint") or contract.recommended_action),
            retryable=bool(data.get("retryable", contract.retryable)),
            details=json_stable(details) if isinstance(details, dict) else {"value": str(details)},
        )


# LLM: ToolCallEnvelope is the v2 machine contract for invoking one tool.
# 类用途: 保存 operation_id、tool_name、schema_version、idempotency_key、status、input 和产物 refs。
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

    # LLM: to_dict serializes a tool call as canonical v2 JSON-ready data.
    # 函数用途: 输出工具调用 envelope，不携带非结构化推断字段。
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

    # LLM: operation_ref builds the lightweight pointer shared by matching result envelopes.
    # 函数用途: 生成 OperationRef，供 ToolResultEnvelope 绑定到调用来源。
    def operation_ref(self) -> OperationRef:
        return OperationRef(
            operation_id=self.operation_id,
            tool_name=self.tool_name,
            idempotency_key=self.idempotency_key,
            schema_version=self.schema_version,
        )


# LLM: ToolResultFailureParams keeps failure creation bundle-shaped.
# 类用途: 保存创建失败结果 envelope 需要的调用、错误、输出、产物和 metadata。
@dataclass(frozen=True)
class ToolResultFailureParams:
    call: ToolCallEnvelope
    error: Any
    output: Any = None
    artifact_refs: list[ArtifactRef] | None = None
    metadata: dict[str, Any] | None = None


# LLM: ToolResultEnvelope is the v2 machine contract for one tool execution result.
# 类用途: 保存 operation_id、tool_name、status、output、ToolError、retry_hint 和 artifact refs。
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

    # LLM: success creates a result envelope from an already normalized call envelope.
    # 函数用途: 统一成功结果字段，避免调用方手写 operation_ref 和状态。
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

    # LLM: failure creates a classified failure envelope from a bundle request.
    # 函数用途: 统一失败状态和 ToolError 分类，方便上层按 error_type/retry_hint 恢复。
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

    # LLM: to_dict serializes result data with structured error and artifact refs.
    # 函数用途: 输出 JSON-ready 结果 envelope；普通 output 文本不会被解析成 refs。
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


# LLM: _error_contract_for keeps ToolError.from_payload short and taxonomy-only.
# 函数用途: 根据显式 error_type 或错误信息返回稳定错误合同。
def _error_contract_for(explicit_type: str, message: str):
    if explicit_type and explicit_type in ERROR_CONTRACTS:
        return error_contract(explicit_type)
    if explicit_type:
        return error_contract("UNKNOWN_ERROR")
    return classify_error(message)


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
