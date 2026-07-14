
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "conversation_thread.v2"


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
    task_path: str = ""

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
            task_path=str(data.get("task_path") or ""),
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
    compacted_through_message_id: str = ""
    compacted_through_byte_offset: int = 0
    compact_generation: int = 0
    compact_updated_at: float = 0.0
    compact_source_messages: int = 0
    verbose_level: str = "off"
    created_at: float = 0.0
    updated_at: float = 0.0
    channel_bindings: tuple[ChannelBinding, ...] = ()
    task_ids: tuple[str, ...] = ()
    active_task_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["channel_bindings"] = [item.to_dict() for item in self.channel_bindings]
        payload["task_ids"] = list(self.task_ids)
        payload["active_task_ids"] = list(self.active_task_ids)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationThread:
        bindings = data.get("channel_bindings")
        active_task_ids = data.get("active_task_ids")
        task_ids = data.get("task_ids")
        metadata = data.get("metadata")
        return cls(
            thread_id=str(data.get("thread_id") or ""),
            canonical_user_id=str(data.get("canonical_user_id") or ""),
            owner_id=str(data.get("owner_id") or ""),
            owner_home=str(data.get("owner_home") or ""),
            title=str(data.get("title") or ""),
            status=str(data.get("status") or "active"),
            summary=str(data.get("summary") or ""),
            compacted_through_message_id=str(
                data.get("compacted_through_message_id") or ""
            ),
            compacted_through_byte_offset=max(
                0,
                int(data.get("compacted_through_byte_offset") or 0),
            ),
            compact_generation=max(0, int(data.get("compact_generation") or 0)),
            compact_updated_at=float(data.get("compact_updated_at") or 0.0),
            compact_source_messages=max(0, int(data.get("compact_source_messages") or 0)),
            verbose_level=_verbose_level(data.get("verbose_level")),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            channel_bindings=tuple(
                ChannelBinding.from_dict(item)
                for item in (bindings if isinstance(bindings, list) else [])
                if isinstance(item, dict)
            ),
            # 旧数据只有 active_task_ids；首次读取时把它同时视为历史索引，
            # 后续写回便自然升级，不需要破坏性迁移。
            task_ids=tuple(
                str(item)
                for item in (
                    task_ids
                    if isinstance(task_ids, list)
                    else active_task_ids
                    if isinstance(active_task_ids, list)
                    else []
                )
            ),
            active_task_ids=tuple(
                str(item) for item in (active_task_ids if isinstance(active_task_ids, list) else [])
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


def _verbose_level(value: object) -> str:
    level = str(value or "off").strip().lower()
    return level if level in {"off", "on", "full"} else "off"


@dataclass(frozen=True)
class BackgroundMainAgentReport:
    thread_id: str
    task_id: str
    reason: str
    response: str
    route_channel: str
    route_target: str
    created_at: float
    # 本轮工具调用的结构化统计(§6-B4 无进展退避的判据来源:零成功调用=无进展轮)。
    tool_call_count: int = 0
    tool_success_count: int = 0


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
    "GUIDANCE_TARGET_TYPES",
    "GuidanceEntry",
    "MessageLogEntry",
    "ObservationEvent",
    "ProgressPolicy",
    "SCHEMA_VERSION",
    "ThreadTaskLink",
    "WakeSignal",
    "new_id",
    "normalize_guidance_target_type",
]


# ---------------------------------------------------------------------------
# Guidance model (lightweight — imported by guidance_tool to avoid circular deps)
# ---------------------------------------------------------------------------

GUIDANCE_TARGET_TYPES = {"agent_run", "thread", "task", "case", "request"}


@dataclass(frozen=True)
class GuidanceEntry:
    guidance_id: str
    target_type: str
    target_id: str
    message: str
    sender: str = ""
    priority: str = "normal"
    delivery: str = "next_turn"
    created_at: float = 0.0
    delivered_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuidanceEntry:
        metadata = data.get("metadata")
        return cls(
            guidance_id=str(data.get("guidance_id") or ""),
            target_type=str(data.get("target_type") or ""),
            target_id=str(data.get("target_id") or ""),
            message=str(data.get("message") or ""),
            sender=str(data.get("sender") or ""),
            priority=str(data.get("priority") or "normal"),
            delivery=str(data.get("delivery") or "next_turn"),
            created_at=float(data.get("created_at") or 0.0),
            delivered_at=float(data.get("delivered_at") or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


def normalize_guidance_target_type(value: object) -> str:
    text = str(value or "").strip()
    return text if text in GUIDANCE_TARGET_TYPES else ""
