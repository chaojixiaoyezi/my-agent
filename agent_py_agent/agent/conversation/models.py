
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "conversation_thread.v3"

THREAD_TASK_LINK_ACTIVE_STATUS = "active"
THREAD_TASK_LINK_INACTIVE_STATUSES = frozenset(
    {
        "abandoned",
        "cancelled",
        "channel_error",
        "completed",
        "done",
        "failed",
        "superseded",
        "taken_over",
        "timeout",
    }
)
# interrupted remains selectable for an explicit user resume, but no runner may
# treat it as implicit authority to restart an old task tree.
THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES = frozenset(
    {*THREAD_TASK_LINK_INACTIVE_STATUSES, "interrupted"}
)

# Runtime lifecycle events that belong in the active root turn's structured
# input queue. The scheduler and tool-loop safe-point gate share this authority.
SUBAGENT_LIFECYCLE_WAKE_REASONS = frozenset(
    {
        "subagent_runner_finished",
        "subagent_capability_request_open",
        "subagent_capability_granted",
    }
)


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


# LLM: ConversationThread is the sole durable authority for transcript, compact cursor, and the
# sticky root workspace selected for later turns; task lifecycle remains in ThreadTaskLink.
# 类用途: 保存一个用户会话的长期状态，其中 workspace_task_id 像 会话运行时 的线程工作目录一样跨轮继承。
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
    workspace_task_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: Persist the v3 sticky workspace id beside the historical/active task indexes.
    # 函数用途: 将完整会话状态写成可跨进程读取的 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["channel_bindings"] = [item.to_dict() for item in self.channel_bindings]
        payload["task_ids"] = list(self.task_ids)
        payload["active_task_ids"] = list(self.active_task_ids)
        return payload

    # LLM: Older v1/v2 records intentionally load with no sticky workspace; gateway migration may
    # derive only an unambiguous exact task and never guesses from prompt text.
    # 函数用途: 兼容读取旧会话记录；旧记录没有 workspace_task_id 时保持为空。
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
            workspace_task_id=str(data.get("workspace_task_id") or ""),
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
    # 后台主代理可以内部推进但不必把每个子任务的碎片回复写进普通聊天。
    delivery_status: str = "sent"
    delivery_reason: str = ""


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
    "SUBAGENT_LIFECYCLE_WAKE_REASONS",
    "ThreadTaskLink",
    "ThreadGoal",
    "THREAD_GOAL_OBJECTIVE_MAX_CHARS",
    "THREAD_GOAL_STATUSES",
    "THREAD_TASK_LINK_ACTIVE_STATUS",
    "THREAD_TASK_LINK_INACTIVE_STATUSES",
    "THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES",
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


THREAD_GOAL_STATUSES = frozenset(
    {"active", "paused", "blocked", "usage_limited", "budget_limited", "complete"}
)
THREAD_GOAL_OBJECTIVE_MAX_CHARS = 4000


# LLM: A thread goal is a persistent execution overlay on one existing conversation, never a second chat/session.
# 类用途: 保存 `/goal` 的目标、状态和对应任务引用，支持自动续跑、暂停、恢复和完成。
@dataclass(frozen=True)
class ThreadGoal:
    goal_id: str
    thread_id: str
    objective: str
    task_id: str
    status: str = "active"
    token_budget: int | None = None
    tokens_used: int = 0
    time_used_seconds: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: Goal records cross process boundaries as plain JSON with no model-derived status aliases.
    # 函数用途: 把目标状态转换成持久化字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: The model and user receive the 会话运行时 protocol projection; scheduler-only ids stay private.
    # 函数用途: 返回与 会话运行时 ThreadGoal 一致的公开字段，不泄露 my-agent 内部任务编号。
    def public_dict(self) -> dict[str, Any]:
        payload = {
            "threadId": self.thread_id,
            "objective": self.objective,
            "status": self.status,
            "tokensUsed": self.tokens_used,
            "timeUsedSeconds": self.time_used_seconds,
            "createdAt": int(self.created_at),
            "updatedAt": int(self.updated_at),
        }
        if self.token_budget is not None:
            payload["tokenBudget"] = self.token_budget
        return payload

    # LLM: Unknown statuses remain visible for fail-closed validation in the store; they are not silently normalized.
    # 函数用途: 从目标 JSON 恢复强类型记录。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadGoal:
        metadata = data.get("metadata")
        return cls(
            goal_id=str(data.get("goal_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            objective=str(data.get("objective") or ""),
            task_id=str(data.get("task_id") or ""),
            status=str(data.get("status") or "active"),
            token_budget=(
                int(data["token_budget"])
                if data.get("token_budget") is not None
                else None
            ),
            tokens_used=max(0, int(data.get("tokens_used") or 0)),
            time_used_seconds=max(0, int(data.get("time_used_seconds") or 0)),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
