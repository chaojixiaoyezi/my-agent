from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Notification:

    notification_id: str
    task_id: str
    user_id: str
    session_id: str
    channel: str  # 发起通道：chat/feishu/qq
    message: str
    status: str = "pending"  # pending/delivered/failed/stored
    created_at: float = 0.0
    delivered_at: float = 0.0
    delivery_channel: str = ""  # 实际投递通道

    def __post_init__(self) -> None:
        """初始化默认值。"""
        if self.created_at == 0.0:
            self.created_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Notification:
        """从字典创建通知实例。"""
        return cls(**data)

    def touch_delivered(self, channel: str) -> None:
        """标记为已投递。"""
        self.status = "delivered"
        self.delivered_at = time.time()
        self.delivery_channel = channel

    def touch_stored(self) -> None:
        """标记为离线存储。"""
        self.status = "stored"


@dataclass
class NotificationDelivery:

    notification_id: str
    channel: str
    success: bool
    error: str = ""
    attempted_at: float = 0.0

    def __post_init__(self) -> None:
        """初始化默认值。"""
        if self.attempted_at == 0.0:
            self.attempted_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationDelivery:
        """从字典创建投递记录实例。"""
        return cls(**data)


def generate_notification_id() -> str:
    timestamp = int(time.time())
    random_part = secrets.token_hex(2)  # 4 位十六进制
    return f"notif_{timestamp}_{random_part}"


__all__ = [
    "Notification",
    "NotificationDelivery",
    "generate_notification_id",
]
