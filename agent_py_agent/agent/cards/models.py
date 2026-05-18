from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    WAITING_USER = "waiting_user"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


def new_card_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def now_ts() -> float:
    return time.time()


@dataclass
class CardModel:
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, StrEnum):
                payload[key] = str(value)
        return payload


@dataclass
class SessionCard(CardModel):
    session_id: str = ""
    user_id: str = ""
    channel: str = "chat"
    active_task_ids: list[str] = field(default_factory=list)

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


@dataclass
class WorkerCard(CardModel):
    worker_id: str = ""
    worker_type: str = "task_agent"
    active_task_id: str | None = None
    heartbeat_at: float = field(default_factory=now_ts)
    status: str = "idle"


@dataclass
class LeaseCard(CardModel):
    lease_id: str = ""
    resource_type: str = ""
    resource_id: str = ""
    owner_id: str = ""
    task_id: str | None = None
    expires_at: float = 0.0
    released_at: float | None = None

    @property
    def is_active(self) -> bool:
        return self.released_at is None and self.expires_at > now_ts()

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


@dataclass
class CheckpointCard(CardModel):
    checkpoint_id: str = ""
    task_id: str = ""
    step: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

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


@dataclass
class EventCard(CardModel):
    event_id: str = ""
    task_id: str | None = None
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

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


@dataclass
class NotificationRouteCard(CardModel):
    route_id: str = ""
    task_id: str = ""
    user_id: str = ""
    channel: str = "internal"
    target: str = ""

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


@dataclass
class ProgressPolicyCard(CardModel):
    policy_id: str = ""
    task_id: str = ""
    mode: str = "completion_only"
    interval_seconds: int | None = None

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
