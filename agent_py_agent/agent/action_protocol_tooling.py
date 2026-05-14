# LLM: Tool-call typed protocol envelopes and legacy payload bridge.
# 模块用途: 定义工具调用、工具结果和旧工具 payload 到 envelope 的转换。

from __future__ import annotations

"""Tool protocol envelope layer."""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    RunScope,
    _dict_or_empty,
    _now_iso,
)


# LLM: ToolCallEnvelopePayloadRequest keeps legacy payload conversion bundle-shaped.
# 类用途: 集中保存旧工具 payload 转 typed envelope 所需字段，避免函数参数继续膨胀。
@dataclass(frozen=True)
class ToolCallEnvelopePayloadRequest:
    payload: dict[str, Any]
    call_id: str
    source: str
    scope: RunScope | None = None
    reserved: dict[str, Any] | None = None


# LLM: ToolCallEnvelope is the typed replacement for executable [TOOL_CALL] text blocks.
# 类用途: 保存一次工具调用的 call_id、工具名、参数、来源和运行范围。
@dataclass(frozen=True)
class ToolCallEnvelope:
    call_id: str
    source: str
    tool: str
    args: dict[str, Any]
    scope: RunScope = field(default_factory=RunScope)
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "tool_call"
    created_at: str = field(default_factory=_now_iso)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict preserves exact envelope shape consumed by future executors.
    # 函数用途: 把工具调用 envelope 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    # LLM: from_dict restores a typed tool call from persisted JSON.
    # 函数用途: 从 JSON 字典恢复 ToolCallEnvelope。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolCallEnvelope:
        return cls(
            call_id=str(payload.get("call_id") or ""),
            source=str(payload.get("source") or ""),
            tool=str(payload.get("tool") or ""),
            args=_dict_or_empty(payload.get("args")),
            scope=RunScope.from_dict(payload.get("scope")),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "tool_call"),
            created_at=str(payload.get("created_at") or _now_iso()),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: ToolCallResultEnvelope links tool output back to the call_id that produced it.
# 类用途: 保存工具执行结果、输出引用、错误和范围，避免后续只靠自然语言记录工具结果。
@dataclass(frozen=True)
class ToolCallResultEnvelope:
    call_id: str
    tool: str
    ok: bool
    output_ref: str = ""
    output: str = ""
    error: str = ""
    scope: RunScope = field(default_factory=RunScope)
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "tool_call_result"
    created_at: str = field(default_factory=_now_iso)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes the result envelope with scope metadata.
    # 函数用途: 把工具结果 envelope 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    # LLM: from_dict restores tool result envelopes from ledgers or artifacts.
    # 函数用途: 从 JSON 字典恢复 ToolCallResultEnvelope。
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
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "tool_call_result"),
            created_at=str(payload.get("created_at") or _now_iso()),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: tool_call_envelope_from_payload is the legacy bridge from flat tool dicts.
# 函数用途: 把旧文本协议解析出的 {"tool": "...", ...} 转成 ToolCallEnvelope。
def tool_call_envelope_from_payload(request: ToolCallEnvelopePayloadRequest) -> ToolCallEnvelope:
    payload = request.payload
    tool = str(payload.get("tool") or "").strip()
    args = {key: value for key, value in payload.items() if key != "tool"}
    return ToolCallEnvelope(
        call_id=request.call_id,
        source=request.source,
        tool=tool,
        args=args,
        scope=request.scope or RunScope(),
        reserved=dict(request.reserved or {}),
    )
