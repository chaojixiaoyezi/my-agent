# LLM: 文件结构是离线通知队列契约，字段变更要兼容旧记录。
# 模块用途: 通知 JSON 文件的创建、读取、状态更新和待处理查询。

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .models import Notification, NotificationDelivery, generate_notification_id


# LLM: CreateNotificationRequest 属于 通知系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: CreateNotificationRequest 请求模型，固定进入 通知系统 前需要的字段。
@dataclass(frozen=True)
class CreateNotificationRequest:
    """bundle for creating notification records."""

    task_id: str
    user_id: str
    session_id: str
    channel: str
    message: str


# LLM: NotificationManager 属于 通知系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 管理通知文件队列，负责创建、读取、状态标记和待处理查询。
class NotificationManager:

    # LLM: NotificationManager.__init__ 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 NotificationManager 的依赖、配置和运行期字段。
    def __init__(self, config: AgentConfig):
        self.config = config
        self._notification_root = Path(getattr(config, "notification_store_path", "data/notifications"))
        self._notification_root.mkdir(parents=True, exist_ok=True)

    # LLM: NotificationManager._get_notification_path 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 get_notification_path 步骤，并保持调用方依赖的数据形状。
    def _get_notification_path(self, notification_id: str) -> Path:
        """获取通知文件路径。"""
        return self._notification_root / f"{notification_id}.json"

    # LLM: NotificationManager.create_notification 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 create_notification 步骤，并保持调用方依赖的数据形状。
    def create_notification(
        self,
        request: CreateNotificationRequest | None = None,
        *,
        task_id: str = "",
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        message: str = "",
    ) -> Notification:
        request = request or CreateNotificationRequest(task_id, user_id, session_id, channel, message)
        notification_id = generate_notification_id()

        notification = Notification(
            notification_id=notification_id,
            task_id=request.task_id,
            user_id=request.user_id,
            session_id=request.session_id,
            channel=request.channel,
            message=request.message,
            status="pending",
            created_at=time.time(),
        )

        self.save_notification(notification)
        return notification

    # LLM: NotificationManager.load_notification 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 读取 load_notification 数据并转换成内部对象。
    def load_notification(self, notification_id: str) -> Notification | None:
        path = self._get_notification_path(notification_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Notification.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    # LLM: NotificationManager.save_notification 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 save_notification 写入本地存储并保留后续查询需要的字段。
    def save_notification(self, notification: Notification) -> None:
        path = self._get_notification_path(notification.notification_id)
        path.write_text(json.dumps(notification.to_dict()), encoding="utf-8")

    # LLM: NotificationManager.mark_delivered 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 mark_delivered 步骤，并保持调用方依赖的数据形状。
    def mark_delivered(self, notification_id: str, channel: str) -> bool:
        notification = self.load_notification(notification_id)
        if notification is None:
            return False

        notification.touch_delivered(channel)
        self.save_notification(notification)
        return True

    # LLM: NotificationManager.mark_stored 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 mark_stored 步骤，并保持调用方依赖的数据形状。
    def mark_stored(self, notification_id: str) -> bool:
        notification = self.load_notification(notification_id)
        if notification is None:
            return False

        notification.touch_stored()
        self.save_notification(notification)
        return True

    # LLM: NotificationManager.mark_failed 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 mark_failed 步骤，并保持调用方依赖的数据形状。
    def mark_failed(self, notification_id: str, error: str) -> bool:
        notification = self.load_notification(notification_id)
        if notification is None:
            return False

        notification.status = "failed"
        self.save_notification(notification)
        return True

    # LLM: NotificationManager.get_pending 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 查询并返回 get_pending，保持返回形状给上层调用。
    def get_pending(self, user_id: str) -> list[Notification]:
        pending: list[Notification] = []

        if not self._notification_root.exists():
            return pending

        for data in self._iter_notification_dicts():
            if data.get("user_id") == user_id and data.get("status") in ("pending", "stored"):
                pending.append(Notification.from_dict(data))

        # 按创建时间排序
        pending.sort(key=lambda n: n.created_at)
        return pending

    # LLM: NotificationManager.get_pending_count 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 查询并返回 get_pending_count，保持返回形状给上层调用。
    def get_pending_count(self, user_id: str) -> int:
        return len(self.get_pending(user_id))

    # LLM: NotificationManager.list_notifications 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 列出符合条件的 list_notifications 结果并遵守数量上限。
    def list_notifications(
        self,
        user_id: str,
        limit: int = 20,
        include_delivered: bool = False,
    ) -> list[Notification]:
        notifications: list[Notification] = []

        if not self._notification_root.exists():
            return notifications

        for data in self._iter_notification_dicts():
            if data.get("user_id") != user_id:
                continue

            status = data.get("status")
            if status == "delivered" and not include_delivered:
                continue

            notifications.append(Notification.from_dict(data))

        # 按创建时间倒序
        notifications.sort(key=lambda n: n.created_at, reverse=True)
        return notifications[:limit]

    # LLM: NotificationManager._iter_notification_dicts 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 iter_notification_dicts 步骤，并保持调用方依赖的数据形状。
    def _iter_notification_dicts(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for path in self._notification_root.iterdir():
            if path.is_file() and path.name.endswith(".json"):
                _append_notification_dict(items, path)
        return items

    # LLM: NotificationManager.delete_notification 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 清理 delete_notification 相关状态，并让调用方知道是否完成。
    def delete_notification(self, notification_id: str) -> bool:
        path = self._get_notification_path(notification_id)
        if not path.exists():
            return False

        path.unlink()
        return True

    # LLM: NotificationManager.notification_exists 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 notification_exists 步骤，并保持调用方依赖的数据形状。
    def notification_exists(self, notification_id: str) -> bool:
        return self._get_notification_path(notification_id).exists()


__all__ = ["NotificationManager"]


# LLM: _append_notification_dict 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 向结果或告警集合加入 append_notification_dict，同时保留调用方依赖的顺序。
def _append_notification_dict(items: list[dict[str, object]], path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return
    if isinstance(data, dict):
        items.append(data)
