
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "conversation_thread.v7"

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

# Audit findings and capacity notices are independently delivered events from a
# detached named workload.  They remain in the owner-visible transcript and in
# the Audit ledgers, but they are not prior replies authored by the foreground
# conversation agent.  Prompt/compact projections use this typed metadata
# boundary instead of inspecting message prose.
AUDIT_BACKGROUND_TRANSCRIPT_REASONS = frozenset(
    {
        "audit_capacity_alert",
        "audit_finding",
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


# LLM: This immutable projection is the only provider-facing seed for completed conversation
# history. It carries already-bounded raw turns plus one committed Compact summary; runtime
# adapters may change role encoding but must not re-read or re-window the transcript.
# 类用途: 把已经由会话层裁定好的摘要和完整历史消息交给模型运行时，避免再拼成每轮变化的大段字符串。
@dataclass(frozen=True)
class ConversationHistorySeed:
    compact_summary: str = ""
    compact_generation: int = 0
    messages: tuple[tuple[str, str], ...] = ()
    canonical_messages: tuple[dict[str, Any], ...] = ()


def is_audit_background_transcript_entry(entry: MessageLogEntry) -> bool:
    """Return whether one visible transcript row belongs to detached Audit delivery."""

    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    return (
        entry.role == "assistant"
        and bool(str(metadata.get("task_id") or "").strip())
        and str(metadata.get("reason") or "").strip().lower()
        in AUDIT_BACKGROUND_TRANSCRIPT_REASONS
        and bool(str(metadata.get("background_delivery_reason") or "").strip())
    )


@dataclass(frozen=True)
class ThreadTaskLink:
    thread_id: str
    task_id: str
    goal: str
    status: str = "active"
    created_at: float = 0.0
    task_path: str = ""
    work_kind: str = ""
    work_name: str = ""
    duration_seconds: int | None = None
    expires_at: float | None = None
    cancellation_scope: str = "foreground"
    # A detached named task continues from one exact point in the shared
    # conversation ledger.  This is a fork boundary, not a second transcript or
    # compact store: later ordinary messages stay in the same thread but are not
    # silently reinterpreted as instructions for the detached task.
    context_anchor_message_id: str = ""
    # ``goal`` is the effective Audit objective.  Prepare turns write only the
    # pending field; one explicit structured publish advances the revision.
    pending_prompt: str = ""
    pending_updated_at: float = 0.0
    # Exact Gateway turn that most recently appended pending prepare text.
    # This prevents a stale/recovered turn from consuming a newer user's
    # pending revision and permits a successful call to be amended only by the
    # same still-running prepare turn.
    pending_prepare_request_id: str = ""
    # Exact user-authored prompt from the most recently published prepare turn.
    # ``goal`` may also contain model-validated operational notes for workers;
    # user-facing status must not present that derived prose as the user's words.
    effective_user_prompt: str = ""
    effective_revision: int = 0
    effective_updated_at: float = 0.0
    effective_prepare_request_id: str = ""
    effective_evidence_refs: tuple[str, ...] = ()
    # Open-world transport facts verified during prepare.  Business meaning
    # remains in ``goal``; these rows only let the runtime hand one exact
    # source to one worker without asking another model turn to rewrite it.
    effective_source_bindings: tuple[dict[str, Any], ...] = ()
    # One named Audit keeps one durable task/workspace identity across runs.
    # Every explicit start increments this host-owned epoch so a later run gets
    # fresh worker attempts, leases, deadlines and traceable run facts. Source
    # cursors/checkpoints and append-only evidence remain on the stable named
    # Audit source identity, so already-ACKed history is not replayed.
    run_epoch: int = 0
    # Exact ordinary-language body of the current explicit start command.
    # It is run-scoped execution context, not published source configuration:
    # on conflict the prepared ``goal`` remains authoritative.
    run_prompt: str = ""

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
            work_kind=str(data.get("work_kind") or ""),
            work_name=str(data.get("work_name") or ""),
            duration_seconds=(
                max(1, int(data["duration_seconds"]))
                if data.get("duration_seconds") is not None
                else None
            ),
            expires_at=(
                float(data["expires_at"])
                if data.get("expires_at") is not None
                else None
            ),
            cancellation_scope=str(data.get("cancellation_scope") or "foreground"),
            context_anchor_message_id=str(
                data.get("context_anchor_message_id") or ""
            ),
            pending_prompt=str(data.get("pending_prompt") or ""),
            pending_updated_at=float(data.get("pending_updated_at") or 0.0),
            pending_prepare_request_id=str(
                data.get("pending_prepare_request_id") or ""
            ),
            effective_user_prompt=str(data.get("effective_user_prompt") or ""),
            effective_revision=max(0, int(data.get("effective_revision") or 0)),
            effective_updated_at=float(data.get("effective_updated_at") or 0.0),
            effective_prepare_request_id=str(
                data.get("effective_prepare_request_id") or ""
            ),
            effective_evidence_refs=tuple(
                str(item)
                for item in (data.get("effective_evidence_refs") or [])
                if str(item)
            ),
            effective_source_bindings=tuple(
                dict(item)
                for item in (data.get("effective_source_bindings") or [])
                if isinstance(item, dict)
            ),
            run_epoch=max(0, int(data.get("run_epoch") or 0)),
            run_prompt=str(data.get("run_prompt") or ""),
        )


def thread_task_run_started_at(link: object, *, fallback: float = 0.0) -> float:
    """Return the typed start edge of the current finite named-work run.

    ``created_at`` is the durable identity's creation time and may precede an
    Audit run by many prepare turns.  Activation atomically writes the run's
    ``duration_seconds`` and ``expires_at``; their difference is therefore the
    authoritative start edge without adding a second lifecycle clock.  Legacy
    links without those facts retain their historical fallback behavior.
    """
    try:
        expires_at = float(getattr(link, "expires_at", 0.0) or 0.0)
        duration = int(getattr(link, "duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        return max(0.0, float(fallback or 0.0))
    if expires_at > 0 and duration > 0:
        return max(0.0, expires_at - duration)
    return max(0.0, float(fallback or 0.0))


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


# LLM: This immutable command carries one already-validated compact candidate into the store;
# ConversationThread remains the persisted authority after the command is applied.
# 类用途: 把摘要、checkpoint 和精确游标作为一个整体交给存储层，避免一串参数彼此错配。
@dataclass(frozen=True)
class ConversationCompactCommit:
    summary: str
    operation_evidence: dict[str, object]
    checkpoint_id: str
    compacted_through_message_id: str
    compacted_through_byte_offset: int
    source_messages: int
    source_tool_pairs: int


# LLM: ConversationThread is the sole durable authority for transcript, compact cursor/checkpoint,
# compact failure circuit, and the sticky root workspace; task lifecycle remains in ThreadTaskLink.
# 类用途: 保存一个用户会话的长期状态，其中 compact 提交点、连续失败和工作目录都随同一 thread 跨轮继承。
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
    compact_source_tool_pairs: int = 0
    compact_checkpoint_id: str = ""
    compact_consecutive_failures: int = 0
    compact_failure_updated_at: float = 0.0
    compact_failure_code: str = ""
    # LLM: LLM summary prose cannot be the authority for whether a compacted
    # assistant turn actually executed a side effect.  This bounded public
    # ledger is advanced atomically with the compact cursor and injected beside
    # the summary on later turns.
    # 字段用途: 保存已压缩历史中的结构化操作核验证据；不从摘要或聊天正文反向推断。
    compact_operation_evidence: dict[str, Any] = field(default_factory=dict)
    verbose_level: str = "off"
    created_at: float = 0.0
    updated_at: float = 0.0
    channel_bindings: tuple[ChannelBinding, ...] = ()
    task_ids: tuple[str, ...] = ()
    active_task_ids: tuple[str, ...] = ()
    workspace_task_id: str = ""
    # 会话运行时 project cwd is thread state, while workspace_task_id points to the hidden
    # durable task workspace. Keeping them separate lets one Gateway serve many TUI directories.
    cwd: str = ""
    runtime_workspace_roots: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: Persist v7 transcript/tool compact sources, guards, sticky workspace and client cwd together.
    # 函数用途: 将完整会话状态写成可跨进程读取的 JSON 字典，并保存两类压缩来源与客户端工作目录。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["channel_bindings"] = [item.to_dict() for item in self.channel_bindings]
        payload["task_ids"] = list(self.task_ids)
        payload["active_task_ids"] = list(self.active_task_ids)
        return payload

    # LLM: Older records load with zero live-tool compact sources and no guessed execution state.
    # 函数用途: 兼容读取旧会话记录；缺少工具压缩数、checkpoint、失败状态或客户端目录时使用安全空值。
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
            compact_source_tool_pairs=max(
                0,
                int(data.get("compact_source_tool_pairs") or 0),
            ),
            compact_checkpoint_id=str(data.get("compact_checkpoint_id") or ""),
            compact_consecutive_failures=max(
                0,
                int(data.get("compact_consecutive_failures") or 0),
            ),
            compact_failure_updated_at=float(
                data.get("compact_failure_updated_at") or 0.0
            ),
            compact_failure_code=str(data.get("compact_failure_code") or ""),
            compact_operation_evidence=(
                data.get("compact_operation_evidence")
                if isinstance(data.get("compact_operation_evidence"), dict)
                else {}
            ),
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
            cwd=str(data.get("cwd") or ""),
            runtime_workspace_roots=_runtime_workspace_roots(
                data.get("runtime_workspace_roots")
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


def _verbose_level(value: object) -> str:
    level = str(value or "off").strip().lower()
    return level if level in {"off", "on", "full"} else "off"


# LLM: JSON reads produce lists while in-process atomic updates may return tuples before a reload;
# both forms represent the same immutable thread root set and must round-trip identically.
# 函数用途: 将会话工作区根字段规范化成去空的不可变字符串元组。
def _runtime_workspace_roots(value: object) -> tuple[str, ...]:
    items = value if isinstance(value, (list, tuple)) else ()
    return tuple(str(item) for item in items if str(item or "").strip())


# LLM: One background slice report separates delivery receipt from exact durable task lifecycle;
# scheduler callers must never equate a model response with completion while task_status is active.
# 类用途: 汇总后台主代理一片工作的回复、工具统计、投递结果和结构化任务状态。
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
    # 只统计 ToolRuntimePolicy 明示为 mutating/dangerous 的成功调用；只读成功不能
    # 伪装成任务推进并持续清空后台退避。
    material_progress_count: int = 0
    # 后台主代理可以内部推进但不必把每个子任务的碎片回复写进普通聊天。
    delivery_status: str = "sent"
    delivery_reason: str = ""
    # A durable wake remains pending until its required side effect has a
    # committed receipt. Ordinary background turns keep the default.
    wake_handled: bool = True
    # The exact conversation task link is the scheduler's completion authority. A model slice may
    # return while this remains active because durable child/lifecycle work will continue later.
    task_status: str = ""


# LLM: One immutable event preserves the model-call summary produced by one
# finalized turn under its exact owner-scoped thread. It contains counters only,
# never prompts, responses, provider credentials, or lifecycle decisions.
# 类用途: 把主代理、后台续作和子代理每一轮的真实模型用量追加到所属会话账本。
@dataclass(frozen=True)
class ThreadModelUsageEvent:
    event_id: str
    thread_id: str
    request_id: str
    run_id: str
    task_id: str
    source: str
    model_calls: dict[str, Any]
    created_at: float = 0.0

    # LLM: Serialization keeps the schema marker beside every append-only row
    # so future migrations never infer record shape from its directory name.
    # 函数用途: 将一次会话模型用量事件转换成可持久化字典。
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "thread_model_usage_event.v1",
            "event_id": self.event_id,
            "thread_id": self.thread_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "source": self.source,
            "model_calls": dict(self.model_calls),
            "created_at": self.created_at,
        }

    # LLM: Usage rows fail closed on unknown schema or missing identity; a
    # corrupt row must never be treated as zero spend.
    # 函数用途: 从持久化字典恢复一次模型用量事件并校验关键字段。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadModelUsageEvent:
        model_calls = data.get("model_calls")
        event = cls(
            event_id=str(data.get("event_id") or "").strip(),
            thread_id=str(data.get("thread_id") or "").strip(),
            request_id=str(data.get("request_id") or "").strip(),
            run_id=str(data.get("run_id") or "").strip(),
            task_id=str(data.get("task_id") or "").strip(),
            source=str(data.get("source") or "").strip(),
            model_calls=dict(model_calls) if isinstance(model_calls, dict) else {},
            created_at=float(data.get("created_at") or 0.0),
        )
        if (
            str(data.get("schema_version") or "") != "thread_model_usage_event.v1"
            or not event.event_id
            or not event.thread_id
            or not event.request_id
            or event.model_calls.get("schema") != "model_call_summary.v1"
        ):
            raise ValueError("thread model usage event is invalid")
        return event


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
    "ThreadModelUsageEvent",
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
    name: str = ""
    status: str = "active"
    token_budget: int | None = None
    duration_seconds: int | None = None
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
        if self.name:
            payload["name"] = self.name
        if self.token_budget is not None:
            payload["tokenBudget"] = self.token_budget
        if self.duration_seconds is not None:
            payload["durationSeconds"] = self.duration_seconds
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
            name=str(data.get("name") or ""),
            status=str(data.get("status") or "active"),
            token_budget=(
                int(data["token_budget"])
                if data.get("token_budget") is not None
                else None
            ),
            duration_seconds=(
                max(1, int(data["duration_seconds"]))
                if data.get("duration_seconds") is not None
                else None
            ),
            tokens_used=max(0, int(data.get("tokens_used") or 0)),
            time_used_seconds=max(0, int(data.get("time_used_seconds") or 0)),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
