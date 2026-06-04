
from __future__ import annotations

"""Typed action protocol public API.

业务代码从 `agent.action_protocol` 导入协议类型；具体定义拆到小文件里，
避免协议层变成超大文件。自然语言回复不在这里获得执行权。
"""

from typing import Any

from .action_protocol_compact import CompactContinuePacketEnvelope
from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    ArtifactRef,
    EvidenceRef,
    PathRef,
    RunScope,
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
