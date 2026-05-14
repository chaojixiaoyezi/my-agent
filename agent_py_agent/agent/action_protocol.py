# LLM: Compatibility facade for the typed action protocol modules.
# 模块用途: 统一导出工具、子代理、compact 和 refs envelope，并提供 kind-based decode 入口。

from __future__ import annotations

"""Typed action protocol public facade.

给人看的解释：
业务代码继续从 `agent.action_protocol` 导入即可；真实定义拆到小文件里，
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


# LLM: decode_action_envelope dispatches JSON payloads to the matching typed class.
# 函数用途: 根据 kind 字段恢复对应 envelope；未知 kind 明确报错，防止静默误执行。
def decode_action_envelope(
    payload: dict[str, Any],
) -> (
    ToolCallEnvelope
    | ToolCallResultEnvelope
    | SubagentResultEnvelope
    | CompactContinuePacketEnvelope
    | SubagentScheduleEnvelope
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
    raise ValueError(f"Unknown action envelope kind: {kind or '<missing>'}")


__all__ = [
    "ACTION_PROTOCOL_SCHEMA_VERSION",
    "ArtifactRef",
    "CompactContinuePacketEnvelope",
    "EvidenceRef",
    "PathRef",
    "RunScope",
    "SubagentResultEnvelope",
    "SubagentScheduleEnvelope",
    "ToolCallEnvelope",
    "ToolCallEnvelopePayloadRequest",
    "ToolCallResultEnvelope",
    "decode_action_envelope",
    "path_refs_from_subagent_refs",
    "subagent_schedule_envelope_from_payload",
    "tool_call_envelope_from_payload",
]
