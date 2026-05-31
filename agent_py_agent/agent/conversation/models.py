# LLM: Conversation models are durable control-plane facts, not task templates.
# 模块用途: 定义长期会话、跨渠道绑定、消息流水、任务绑定和进度策略的数据结构。

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "conversation_thread.v1"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class ChannelBinding:
    channel: str
    channel_conversation_id: str
    channel_user_id: str
    canonical_user_id: str
    thread_id: str
    last_active_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelBinding:
        return cls(
            channel=str(data.get("channel") or ""),
            channel_conversation_id=str(data.get("channel_conversation_id") or ""),
            channel_user_id=str(data.get("channel_user_id") or ""),
            canonical_user_id=str(data.get("canonical_user_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            last_active_at=float(data.get("last_active_at") or 0.0),
        )


@dataclass(frozen=True)
class MessageLogEntry:
    message_id: str
    thread_id: str
    role: str
    content: str
    channel: str = "internal"
    channel_message_id: str = ""
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageLogEntry:
        metadata = data.get("metadata")
        return cls(
            message_id=str(data.get("message_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            role=str(data.get("role") or ""),
            content=str(data.get("content") or ""),
            channel=str(data.get("channel") or "internal"),
            channel_message_id=str(data.get("channel_message_id") or ""),
            created_at=float(data.get("created_at") or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class ThreadTaskLink:
    thread_id: str
    task_id: str
    goal: str
    status: str = "active"
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadTaskLink:
        return cls(
            thread_id=str(data.get("thread_id") or ""),
            task_id=str(data.get("task_id") or ""),
            goal=str(data.get("goal") or ""),
            status=str(data.get("status") or "active"),
            created_at=float(data.get("created_at") or 0.0),
        )


@dataclass(frozen=True)
class ProgressPolicy:
    policy_id: str
    thread_id: str
    task_id: str
    interval_seconds: int
    next_due_at: float
    route_channel: str = "internal"
    route_target: str = ""
    enabled: bool = True
    last_report_at: float = 0.0
    report_on_blocked: bool = True
    report_on_completion: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressPolicy:
        metadata = data.get("metadata")
        return cls(
            policy_id=str(data.get("policy_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            task_id=str(data.get("task_id") or ""),
            interval_seconds=max(0, int(data.get("interval_seconds") or 0)),
            next_due_at=float(data.get("next_due_at") or 0.0),
            route_channel=str(data.get("route_channel") or "internal"),
            route_target=str(data.get("route_target") or ""),
            enabled=bool(data.get("enabled", True)),
            last_report_at=float(data.get("last_report_at") or 0.0),
            report_on_blocked=bool(data.get("report_on_blocked", True)),
            report_on_completion=bool(data.get("report_on_completion", True)),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class ConversationThread:
    thread_id: str
    canonical_user_id: str
    owner_id: str = ""
    owner_home: str = ""
    title: str = ""
    status: str = "active"
    summary: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    channel_bindings: tuple[ChannelBinding, ...] = ()
    active_task_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["channel_bindings"] = [item.to_dict() for item in self.channel_bindings]
        payload["active_task_ids"] = list(self.active_task_ids)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationThread:
        bindings = data.get("channel_bindings")
        active_task_ids = data.get("active_task_ids")
        metadata = data.get("metadata")
        return cls(
            thread_id=str(data.get("thread_id") or ""),
            canonical_user_id=str(data.get("canonical_user_id") or ""),
            owner_id=str(data.get("owner_id") or ""),
            owner_home=str(data.get("owner_home") or ""),
            title=str(data.get("title") or ""),
            status=str(data.get("status") or "active"),
            summary=str(data.get("summary") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            channel_bindings=tuple(
                ChannelBinding.from_dict(item)
                for item in (bindings if isinstance(bindings, list) else [])
                if isinstance(item, dict)
            ),
            active_task_ids=tuple(
                str(item) for item in (active_task_ids if isinstance(active_task_ids, list) else [])
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class BackgroundMainAgentReport:
    thread_id: str
    task_id: str
    reason: str
    response: str
    route_channel: str
    route_target: str
    created_at: float


@dataclass(frozen=True)
class ObservationEvent:
    observation_id: str
    thread_id: str
    event_type: str
    summary: str
    urgency: str = "normal"
    severity: str = ""
    source_agent_id: str = ""
    parent_agent_id: str = ""
    root_task_id: str = ""
    evidence_refs: tuple[str, ...] = ()
    requires_main_agent: bool = False
    requires_llm_report: bool = False
    observed_at: float = 0.0
    handled_at: float = 0.0
    wake_signal_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ObservationEvent:
        metadata = data.get("metadata")
        refs = data.get("evidence_refs")
        return cls(
            observation_id=str(data.get("observation_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            event_type=str(data.get("event_type") or ""),
            summary=str(data.get("summary") or ""),
            urgency=str(data.get("urgency") or "normal"),
            severity=str(data.get("severity") or ""),
            source_agent_id=str(data.get("source_agent_id") or ""),
            parent_agent_id=str(data.get("parent_agent_id") or ""),
            root_task_id=str(data.get("root_task_id") or ""),
            evidence_refs=tuple(str(item) for item in (refs if isinstance(refs, list) else [])),
            requires_main_agent=bool(data.get("requires_main_agent", False)),
            requires_llm_report=bool(data.get("requires_llm_report", False)),
            observed_at=float(data.get("observed_at") or 0.0),
            handled_at=float(data.get("handled_at") or 0.0),
            wake_signal_id=str(data.get("wake_signal_id") or ""),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class WakeSignal:
    wake_signal_id: str
    thread_id: str
    observation_id: str = ""
    urgency: str = "urgent"
    severity: str = ""
    reason: str = ""
    source_agent_id: str = ""
    parent_agent_id: str = ""
    root_task_id: str = ""
    summary: str = ""
    evidence_refs: tuple[str, ...] = ()
    created_at: float = 0.0
    handled_at: float = 0.0
    status: str = "pending"
    dedupe_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WakeSignal:
        metadata = data.get("metadata")
        refs = data.get("evidence_refs")
        return cls(
            wake_signal_id=str(data.get("wake_signal_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            observation_id=str(data.get("observation_id") or ""),
            urgency=str(data.get("urgency") or "urgent"),
            severity=str(data.get("severity") or ""),
            reason=str(data.get("reason") or ""),
            source_agent_id=str(data.get("source_agent_id") or ""),
            parent_agent_id=str(data.get("parent_agent_id") or ""),
            root_task_id=str(data.get("root_task_id") or ""),
            summary=str(data.get("summary") or ""),
            evidence_refs=tuple(str(item) for item in (refs if isinstance(refs, list) else [])),
            created_at=float(data.get("created_at") or 0.0),
            handled_at=float(data.get("handled_at") or 0.0),
            status=str(data.get("status") or "pending"),
            dedupe_key=str(data.get("dedupe_key") or ""),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


__all__ = [
    "BackgroundMainAgentReport",
    "ChannelBinding",
    "ConversationThread",
    "MessageLogEntry",
    "ObservationEvent",
    "ProgressPolicy",
    "SCHEMA_VERSION",
    "ThreadTaskLink",
    "WakeSignal",
    "new_id",
]
