from __future__ import annotations

# LLM: Card runtime models define durable task/session/worker facts; keep field names stable for JSON stores.
# 模块用途: 定义 Card runtime 的持久化数据模型，让会话、任务、租约、恢复点和通知路线有统一结构。
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


 # LLM: TaskStatus is serialized into TaskCard files and event payloads; add values only with transition tests.
 # 类用途: 表示任务生命周期状态，给队列、恢复和通知逻辑做稳定判断。
class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    WAITING_USER = "waiting_user"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# LLM: SubagentRunStatus is serialized into SubagentRunCard records and event payloads.
# 类用途: 表示 session 化子代理运行记录的生命周期状态。
class SubagentRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


VALID_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.WAITING_USER, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED},
    TaskStatus.RUNNING: {TaskStatus.BLOCKED, TaskStatus.WAITING_USER, TaskStatus.RETRYING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.QUEUED},
    TaskStatus.BLOCKED: {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_USER, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.WAITING_USER: {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.RETRYING: {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


 # LLM: new_card_id generates local durable ids without depending on prompt text.
 # 函数用途: 生成 Card 层对象 id，避免任务、租约、事件等记录撞名。
def new_card_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


 # LLM: now_ts centralizes wall-clock timestamps for card records.
 # 函数用途: 返回当前时间戳，供 Card 创建、更新和过期判断使用。
def now_ts() -> float:
    return time.time()


 # LLM: CardModel is the shared serialization base for card records.
 # 类用途: 提供创建时间、更新时间、metadata 和 to_dict 序列化能力。
@dataclass
class CardModel:
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: CardModel.to_dict keeps enum values JSON-friendly for file persistence.
    # 函数用途: 把 Card 数据转换成可写入 JSON 的字典。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, StrEnum):
                payload[key] = str(value)
        return payload


 # LLM: SessionCard records the conversation window without doing agent work.
 # 类用途: 保存 session 与用户、渠道、活跃任务之间的绑定关系。
@dataclass
class SessionCard(CardModel):
    session_id: str = ""
    user_id: str = ""
    channel: str = "chat"
    active_task_ids: list[str] = field(default_factory=list)

    # LLM: SessionCard.from_dict accepts persisted JSON while preserving default compatibility.
    # 函数用途: 从磁盘字典恢复 SessionCard，兼容缺省字段。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SessionCard:
        return cls(
            session_id=str(payload.get("session_id", "")),
            user_id=str(payload.get("user_id", "")),
            channel=str(payload.get("channel", "chat")),
            active_task_ids=list(payload.get("active_task_ids") or []),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


 # LLM: TaskCard is the durable task order, not the worker that executes it.
 # 类用途: 记录任务目标、状态、父子关系、验收条件和产物引用。
@dataclass
class TaskCard(CardModel):
    task_id: str = ""
    goal: str = ""
    user_id: str = ""
    session_id: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    parent_task_id: str | None = None
    child_task_ids: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)

    # LLM: TaskCard.from_dict normalizes persisted status strings back to TaskStatus.
    # 函数用途: 从磁盘字典恢复 TaskCard，并把状态字段转成枚举。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskCard:
        return cls(
            task_id=str(payload.get("task_id", "")),
            goal=str(payload.get("goal", "")),
            user_id=str(payload.get("user_id", "")),
            session_id=str(payload.get("session_id", "")),
            status=TaskStatus(str(payload.get("status", TaskStatus.QUEUED))),
            parent_task_id=payload.get("parent_task_id"),
            child_task_ids=list(payload.get("child_task_ids") or []),
            acceptance=list(payload.get("acceptance") or []),
            artifact_refs=list(payload.get("artifact_refs") or []),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


 # LLM: WorkerCard describes an executor heartbeat without owning the task permanently.
 # 类用途: 记录执行者类型、当前任务、心跳和运行状态。
@dataclass
class WorkerCard(CardModel):
    worker_id: str = ""
    worker_type: str = "task_agent"
    active_task_id: str | None = None
    heartbeat_at: float = field(default_factory=now_ts)
    status: str = "idle"


 # LLM: WorkerRunCard records one concrete execution attempt for a logical task.
 # 类用途: 把“哪个 worker 在什么时候跑了哪个 task”落成可恢复、可审计的运行记录。
@dataclass
class WorkerRunCard(CardModel):
    worker_run_id: str = ""
    task_id: str = ""
    worker_id: str = ""
    worker_type: str = "task_agent"
    lease_id: str | None = None
    status: str = "running"
    started_at: float = field(default_factory=now_ts)
    finished_at: float | None = None

    # LLM: WorkerRunCard.from_dict restores execution attempts for recovery dashboards.
    # 函数用途: 从磁盘字典恢复 WorkerRunCard。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkerRunCard:
        return cls(
            worker_run_id=str(payload.get("worker_run_id", "")),
            task_id=str(payload.get("task_id", "")),
            worker_id=str(payload.get("worker_id", "")),
            worker_type=str(payload.get("worker_type", "task_agent")),
            lease_id=payload.get("lease_id"),
            status=str(payload.get("status", "running")),
            started_at=float(payload.get("started_at", now_ts())),
            finished_at=payload.get("finished_at"),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


# LLM: SubagentRunCard mirrors the OpenClaw-style subagent run registry in local card form.
# 类用途: 记录父 session、子 session、任务、产物和最终交付状态，防止子代理假完成或失联。
@dataclass
class SubagentRunCard(CardModel):
    run_id: str = ""
    requester_session_id: str = ""
    child_session_id: str = ""
    controller_session_id: str = ""
    task_id: str = ""
    goal: str = ""
    label: str = ""
    mode: str = "run"
    context_mode: str = "isolated"
    cleanup: str = "keep"
    status: SubagentRunStatus = SubagentRunStatus.CREATED
    outcome: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    expects_completion_message: bool = False
    pending_final_delivery: bool = False
    started_at: float | None = None
    ended_at: float | None = None

    # LLM: SubagentRunCard.from_dict restores persisted subagent run facts for recovery and control-plane reads.
    # 函数用途: 从磁盘字典恢复 session 化子代理运行记录。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubagentRunCard:
        return cls(
            run_id=str(payload.get("run_id", "")),
            requester_session_id=str(payload.get("requester_session_id", "")),
            child_session_id=str(payload.get("child_session_id", "")),
            controller_session_id=str(payload.get("controller_session_id", "")),
            task_id=str(payload.get("task_id", "")),
            goal=str(payload.get("goal", "")),
            label=str(payload.get("label", "")),
            mode=str(payload.get("mode", "run")),
            context_mode=str(payload.get("context_mode", "isolated")),
            cleanup=str(payload.get("cleanup", "keep")),
            status=SubagentRunStatus(str(payload.get("status", SubagentRunStatus.CREATED))),
            outcome=str(payload.get("outcome", "")),
            artifact_refs=list(payload.get("artifact_refs") or []),
            expects_completion_message=bool(payload.get("expects_completion_message", False)),
            pending_final_delivery=bool(payload.get("pending_final_delivery", False)),
            started_at=payload.get("started_at"),
            ended_at=payload.get("ended_at"),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


 # LLM: LeaseCard guards exclusive resource claims across local workers.
 # 类用途: 表示任务、文件、worker slot 等资源的限时占用凭证。
@dataclass
class LeaseCard(CardModel):
    lease_id: str = ""
    resource_type: str = ""
    resource_id: str = ""
    owner_id: str = ""
    task_id: str | None = None
    expires_at: float = 0.0
    released_at: float | None = None

    # LLM: LeaseCard.is_active is the concurrency gate used by stores and worker pools.
    # 函数用途: 判断租约是否仍有效且未释放。
    @property
    def is_active(self) -> bool:
        return self.released_at is None and self.expires_at > now_ts()

    # LLM: LeaseCard.from_dict restores lease state for stale-worker recovery.
    # 函数用途: 从磁盘字典恢复 LeaseCard，保留过期和释放信息。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LeaseCard:
        return cls(
            lease_id=str(payload.get("lease_id", "")),
            resource_type=str(payload.get("resource_type", "")),
            resource_id=str(payload.get("resource_id", "")),
            owner_id=str(payload.get("owner_id", "")),
            task_id=payload.get("task_id"),
            expires_at=float(payload.get("expires_at", 0.0)),
            released_at=payload.get("released_at"),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


 # LLM: CheckpointCard stores resumable task progress separate from chat history.
 # 类用途: 记录任务恢复点、阶段名和恢复所需 payload。
@dataclass
class CheckpointCard(CardModel):
    checkpoint_id: str = ""
    task_id: str = ""
    step: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    # LLM: CheckpointCard.from_dict restores checkpoint payloads for supervisor recovery.
    # 函数用途: 从磁盘字典恢复 CheckpointCard。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CheckpointCard:
        return cls(
            checkpoint_id=str(payload.get("checkpoint_id", "")),
            task_id=str(payload.get("task_id", "")),
            step=str(payload.get("step", "")),
            payload=dict(payload.get("payload") or {}),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


 # LLM: EventCard is the append-only audit fact for task/runtime changes.
 # 类用途: 记录任务创建、状态变化、租约和恢复等事件。
@dataclass
class EventCard(CardModel):
    event_id: str = ""
    task_id: str | None = None
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    # LLM: EventCard.from_dict keeps event log replay tolerant of missing optional fields.
    # 函数用途: 从 JSONL 行恢复 EventCard。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EventCard:
        return cls(
            event_id=str(payload.get("event_id", "")),
            task_id=payload.get("task_id"),
            event_type=str(payload.get("event_type", "")),
            payload=dict(payload.get("payload") or {}),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


 # LLM: NotificationRouteCard stores where completion/progress should return.
 # 类用途: 保存任务通知的用户、渠道和结构化目标地址。
@dataclass
class NotificationRouteCard(CardModel):
    route_id: str = ""
    task_id: str = ""
    user_id: str = ""
    channel: str = "internal"
    target: str = ""

    # LLM: NotificationRouteCard.from_dict restores return-address snapshots for dispatch.
    # 函数用途: 从磁盘字典恢复 NotificationRouteCard。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NotificationRouteCard:
        return cls(
            route_id=str(payload.get("route_id", "")),
            task_id=str(payload.get("task_id", "")),
            user_id=str(payload.get("user_id", "")),
            channel=str(payload.get("channel", "internal")),
            target=str(payload.get("target", "")),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )


 # LLM: ProgressPolicyCard controls feedback cadence without relying on prompt text.
 # 类用途: 记录任务是否只完成通知，或按固定间隔汇报进度。
@dataclass
class ProgressPolicyCard(CardModel):
    policy_id: str = ""
    task_id: str = ""
    mode: str = "completion_only"
    interval_seconds: int | None = None

    # LLM: ProgressPolicyCard.from_dict restores progress cadence after restarts.
    # 函数用途: 从磁盘字典恢复 ProgressPolicyCard。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProgressPolicyCard:
        return cls(
            policy_id=str(payload.get("policy_id", "")),
            task_id=str(payload.get("task_id", "")),
            mode=str(payload.get("mode", "completion_only")),
            interval_seconds=payload.get("interval_seconds"),
            created_at=float(payload.get("created_at", now_ts())),
            updated_at=float(payload.get("updated_at", now_ts())),
            metadata=dict(payload.get("metadata") or {}),
        )
