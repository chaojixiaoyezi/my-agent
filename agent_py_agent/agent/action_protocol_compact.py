# LLM: Compact typed protocol envelope for resume continuation packets.
# 模块用途: 定义 compact/resume 继续工作包的机器可读 envelope。

from __future__ import annotations

"""Compact resume envelope layer."""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    PathRef,
    RunScope,
    _dict_list,
    _dict_or_empty,
    _now_iso,
    _string_list,
)


# LLM: CompactContinuePacketEnvelope is the typed recovery handle after compact/resume.
# 类用途: 保存 compact 后继续工作的关键状态、guard 决策和推荐读取引用，避免从自然语言恢复说明里猜任务状态。
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
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "compact_continue_packet"
    created_at: str = field(default_factory=_now_iso)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes compact recovery packets for resume payloads.
    # 函数用途: 把 CompactContinuePacketEnvelope 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        payload["path_refs"] = [item.to_dict() for item in self.path_refs]
        return payload

    # LLM: from_dict restores compact recovery packets without parsing prose blocks.
    # 函数用途: 从 JSON 字典恢复 CompactContinuePacketEnvelope。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompactContinuePacketEnvelope:
        return cls(
            packet_id=str(payload.get("packet_id") or payload.get("id") or ""),
            apply_id=str(payload.get("apply_id") or ""),
            plan_id=str(payload.get("plan_id") or ""),
            ready_to_continue=bool(payload.get("ready_to_continue")),
            continue_mode=str(payload.get("continue_mode") or ""),
            owner=_dict_or_empty(payload.get("owner")),
            work_state=_dict_or_empty(payload.get("work_state")),
            guard=_dict_or_empty(payload.get("guard")),
            path_refs=[PathRef.from_dict(item) for item in _dict_list(payload.get("path_refs"))],
            next_actions=_string_list(payload.get("next_actions")),
            scope=RunScope.from_dict(payload.get("scope")),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "compact_continue_packet"),
            created_at=str(payload.get("created_at") or _now_iso()),
            reserved=_dict_or_empty(payload.get("reserved")),
        )
