# LLM: 模型字段会写入磁盘并跨进程读取，新增字段需默认兼容。
# 模块用途: 通知和投递记录的数据模型及 ID 生成。

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any


# LLM: Notification 属于 通知系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 单条通知记录，保存接收人、渠道、状态和投递历史。
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
    last_error: str = ""

    # LLM: Notification.__post_init__ 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 补齐 dataclass 的派生默认值，避免调用方处理 None。
    def __post_init__(self) -> None:
        """初始化默认值。"""
        if self.created_at == 0.0:
            self.created_at = time.time()

    # LLM: Notification.to_dict 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把Notification转成可 JSON 持久化的字典。
    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return asdict(self)

    # LLM: Notification.from_dict 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 从持久化字典恢复Notification。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Notification:
        """从字典创建通知实例。"""
        fields = cls.__dataclass_fields__
        return cls(**{name: value for name, value in data.items() if name in fields})

    # LLM: Notification.touch_delivered 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 touch_delivered 步骤，并保持调用方依赖的数据形状。
    def touch_delivered(self, channel: str) -> None:
        """标记为已投递。"""
        self.status = "delivered"
        self.delivered_at = time.time()
        self.delivery_channel = channel

    # LLM: Notification.touch_stored 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 touch_stored 步骤，并保持调用方依赖的数据形状。
    def touch_stored(self) -> None:
        """标记为离线存储。"""
        self.status = "stored"

    # LLM: failed notification state is diagnostic-only and must not imply task failure.
    # 函数用途: 把通知投递异常记录到通知文件，方便排查网关超时等问题。
    def touch_failed(self, error: str) -> None:
        """标记为投递失败，并保留可查询的错误摘要。"""
        self.status = "failed"
        self.last_error = error


# LLM: NotificationDelivery 属于 通知系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 一次通知投递尝试，保存渠道、状态、时间和错误信息。
@dataclass
class NotificationDelivery:

    notification_id: str
    channel: str
    success: bool
    error: str = ""
    attempted_at: float = 0.0

    # LLM: NotificationDelivery.__post_init__ 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 补齐 dataclass 的派生默认值，避免调用方处理 None。
    def __post_init__(self) -> None:
        """初始化默认值。"""
        if self.attempted_at == 0.0:
            self.attempted_at = time.time()

    # LLM: NotificationDelivery.to_dict 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把NotificationDelivery转成可 JSON 持久化的字典。
    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return asdict(self)

    # LLM: NotificationDelivery.from_dict 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 从持久化字典恢复NotificationDelivery。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationDelivery:
        """从字典创建投递记录实例。"""
        return cls(**data)


# LLM: generate_notification_id 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 通知系统 中的 generate_notification_id 步骤，并保持调用方依赖的数据形状。
def generate_notification_id() -> str:
    timestamp = int(time.time())
    random_part = secrets.token_hex(2)  # 4 位十六进制
    return f"notif_{timestamp}_{random_part}"


__all__ = [
    "Notification",
    "NotificationDelivery",
    "generate_notification_id",
]
