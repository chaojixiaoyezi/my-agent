from __future__ import annotations

# LLM: Message models define the internal communication contract; keep target and delivery fields stable.
# 模块用途: 定义内部消息、消息目标和投递记录的数据模型。
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


 # LLM: new_message_id creates durable local ids for messages and deliveries.
 # 函数用途: 生成消息或投递记录 id。
def new_message_id(prefix: str = "msg") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


 # LLM: MessageTarget is the structured address form used by inbox routing.
 # 类用途: 表示 session/user/task/agent/channel 等内部消息目标。
@dataclass(frozen=True)
class MessageTarget:
    kind: str
    identifier: str

    # LLM: MessageTarget.key serializes the address for folders and message payloads.
    # 函数用途: 把消息目标转换成 kind:identifier 字符串。
    @property
    def key(self) -> str:
        return f"{self.kind}:{self.identifier}"

    # LLM: MessageTarget.parse accepts existing target strings without natural-language guessing.
    # 函数用途: 从 kind:identifier 字符串恢复 MessageTarget。
    @classmethod
    def parse(cls, value: str) -> MessageTarget:
        kind, _, identifier = value.partition(":")
        if not kind.strip() or not identifier.strip():
            raise ValueError(f"invalid message target: {value}")
        return cls(kind=kind, identifier=identifier)


 # LLM: MessageCard stores the message body or preview plus routing metadata.
 # 类用途: 保存一条内部消息的发送方、接收方、正文、类型和关联任务。
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

    # LLM: MessageCard.to_dict preserves the persisted message schema.
    # 函数用途: 把 MessageCard 转成可写入 JSON 的字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: MessageCard.from_dict restores persisted messages for inbox reads.
    # 函数用途: 从磁盘字典恢复 MessageCard。
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


 # LLM: DeliveryCard records per-target delivery/read state for idempotent inboxes.
 # 类用途: 保存消息投递到某个目标后的未读、已读和幂等信息。
@dataclass
class DeliveryCard:
    delivery_id: str
    message_id: str
    target: str
    status: str = "unread"
    created_at: float = field(default_factory=time.time)
    read_at: float | None = None
    idempotency_key: str | None = None

    # LLM: DeliveryCard.to_dict preserves the inbox delivery schema.
    # 函数用途: 把 DeliveryCard 转成可写入 JSON 的字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: DeliveryCard.from_dict restores inbox delivery state.
    # 函数用途: 从磁盘字典恢复 DeliveryCard。
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
