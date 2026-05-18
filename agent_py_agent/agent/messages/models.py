from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_message_id(prefix: str = "msg") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class MessageTarget:
    kind: str
    identifier: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.identifier}"

    @classmethod
    def parse(cls, value: str) -> MessageTarget:
        kind, _, identifier = value.partition(":")
        return cls(kind=kind, identifier=identifier)


@dataclass
class MessageCard:
    message_id: str
    sender: str
    target: str
    content: str
    message_type: str = "text"
    task_id: str | None = None
    idempotency_key: str | None = None
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MessageCard:
        return cls(
            message_id=str(payload.get("message_id", "")),
            sender=str(payload.get("sender", "")),
            target=str(payload.get("target", "")),
            content=str(payload.get("content", "")),
            message_type=str(payload.get("message_type", "text")),
            task_id=payload.get("task_id"),
            idempotency_key=payload.get("idempotency_key"),
            created_at=float(payload.get("created_at", time.time())),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class DeliveryCard:
    delivery_id: str
    message_id: str
    target: str
    status: str = "unread"
    created_at: float = field(default_factory=time.time)
    read_at: float | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DeliveryCard:
        return cls(
            delivery_id=str(payload.get("delivery_id", "")),
            message_id=str(payload.get("message_id", "")),
            target=str(payload.get("target", "")),
            status=str(payload.get("status", "unread")),
            created_at=float(payload.get("created_at", time.time())),
            read_at=payload.get("read_at"),
            idempotency_key=payload.get("idempotency_key"),
        )
