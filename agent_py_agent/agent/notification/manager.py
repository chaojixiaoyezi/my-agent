from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .models import Notification, NotificationDelivery, generate_notification_id


class NotificationManager:

    def __init__(self, config: AgentConfig):
        self.config = config
        self._notification_root = Path(getattr(config, "notification_store_path", "data/notifications"))
        self._notification_root.mkdir(parents=True, exist_ok=True)

    def _get_notification_path(self, notification_id: str) -> Path:
        """获取通知文件路径。"""
        return self._notification_root / f"{notification_id}.json"

    def create_notification(
        self,
        task_id: str,
        user_id: str,
        session_id: str,
        channel: str,
        message: str,
    ) -> Notification:
        notification_id = generate_notification_id()

        notification = Notification(
            notification_id=notification_id,
            task_id=task_id,
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            message=message,
            status="pending",
            created_at=time.time(),
        )

        self.save_notification(notification)
        return notification

    def load_notification(self, notification_id: str) -> Notification | None:
        path = self._get_notification_path(notification_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Notification.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def save_notification(self, notification: Notification) -> None:
        path = self._get_notification_path(notification.notification_id)
        path.write_text(json.dumps(notification.to_dict()), encoding="utf-8")

    def mark_delivered(self, notification_id: str, channel: str) -> bool:
        notification = self.load_notification(notification_id)
        if notification is None:
            return False

        notification.touch_delivered(channel)
        self.save_notification(notification)
        return True

    def mark_stored(self, notification_id: str) -> bool:
        notification = self.load_notification(notification_id)
        if notification is None:
            return False

        notification.touch_stored()
        self.save_notification(notification)
        return True

    def mark_failed(self, notification_id: str, error: str) -> bool:
        notification = self.load_notification(notification_id)
        if notification is None:
            return False

        notification.status = "failed"
        self.save_notification(notification)
        return True

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

    def get_pending_count(self, user_id: str) -> int:
        return len(self.get_pending(user_id))

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

    def _iter_notification_dicts(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for path in self._notification_root.iterdir():
            if path.is_file() and path.name.endswith(".json"):
                _append_notification_dict(items, path)
        return items

    def delete_notification(self, notification_id: str) -> bool:
        path = self._get_notification_path(notification_id)
        if not path.exists():
            return False

        path.unlink()
        return True

    def notification_exists(self, notification_id: str) -> bool:
        return self._get_notification_path(notification_id).exists()


__all__ = ["NotificationManager"]


def _append_notification_dict(items: list[dict[str, object]], path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return
    if isinstance(data, dict):
        # LLM: malformed notification files are skipped before model hydration.
        items.append(data)
