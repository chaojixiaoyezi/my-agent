
from __future__ import annotations

"""Typed action protocol public API.

业务代码从 `agent.action_protocol` 导入协议类型。自然语言回复不在这里获得执行权。
"""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    ArtifactRef,
    EvidenceRef,
    PathRef,
    RunScope,
    _default_operation_id,
    _dict_list,
    _dict_or_empty,
    _now_iso,
)
from .action_protocol_subagent_dispatch import (
    SubagentDispatchEnvelope,
    subagent_dispatch_envelope_from_payload,
)
from .action_protocol_subagents import (
    SubagentResultEnvelope,
    SubagentScheduleEnvelope,
    path_refs_from_subagent_refs,
    subagent_schedule_envelope_from_payload,
)
from .action_protocol_tooling import (
    ToolCallEnvelope,
    ToolCallEnvelopePayloadRequest,
    ToolCallResultEnvelope,
    tool_call_envelope_from_payload,
)
from .common.value_parsing import string_list


@dataclass(frozen=True)
class CompactContinuePacketEnvelope:
    packet_id: str
    apply_id: str
    plan_id: str
    ready_to_continue: bool
    continue_mode: str
    owner: dict[str, Any] = field(default_factory=dict)
    work_state: dict[str, Any] = field(default_factory=dict)
    guard: dict[str, Any] = field(default_factory=dict)
    path_refs: list[PathRef] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "compact_continue_packet"
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.packet_id))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        payload["path_refs"] = [item.to_dict() for item in self.path_refs]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompactContinuePacketEnvelope:
        return cls(
            packet_id=str(payload.get("packet_id") or ""),
            apply_id=str(payload.get("apply_id") or ""),
            plan_id=str(payload.get("plan_id") or ""),
            ready_to_continue=bool(payload.get("ready_to_continue")),
            continue_mode=str(payload.get("continue_mode") or ""),
            owner=_dict_or_empty(payload.get("owner")),
            work_state=_dict_or_empty(payload.get("work_state")),
            guard=_dict_or_empty(payload.get("guard")),
            path_refs=[PathRef.from_dict(item) for item in _dict_list(payload.get("path_refs"))],
            next_actions=string_list(payload.get("next_actions")),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "compact_continue_packet"),
            created_at=str(payload.get("created_at") or _now_iso()),
        )


def decode_action_envelope(
    payload: dict[str, Any],
) -> (
    ToolCallEnvelope
    | ToolCallResultEnvelope
    | SubagentResultEnvelope
    | CompactContinuePacketEnvelope
    | SubagentScheduleEnvelope
    | SubagentDispatchEnvelope
):
    kind = str(payload.get("kind") or "")
    if kind == "tool_call":
        return ToolCallEnvelope.from_dict(payload)
    if kind == "tool_call_result":
        return ToolCallResultEnvelope.from_dict(payload)
    if kind == "subagent_result":
        return SubagentResultEnvelope.from_dict(payload)
    if kind == "compact_continue_packet":
        return CompactContinuePacketEnvelope.from_dict(payload)
    if kind == "subagent_schedule":
        return SubagentScheduleEnvelope.from_dict(payload)
    if kind == "subagent_dispatch":
        return SubagentDispatchEnvelope.from_dict(payload)
    raise ValueError(f"Unknown action envelope kind: {kind or '<missing>'}")


__all__ = [
    "ACTION_PROTOCOL_SCHEMA_VERSION",
    "ArtifactRef",
    "CompactContinuePacketEnvelope",
    "EvidenceRef",
    "PathRef",
    "RunScope",
    "SubagentDispatchEnvelope",
    "SubagentResultEnvelope",
    "SubagentScheduleEnvelope",
    "ToolCallEnvelope",
    "ToolCallEnvelopePayloadRequest",
    "ToolCallResultEnvelope",
    "decode_action_envelope",
    "path_refs_from_subagent_refs",
    "subagent_dispatch_envelope_from_payload",
    "subagent_schedule_envelope_from_payload",
    "tool_call_envelope_from_payload",
]
