
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .channel_status import ChannelStatusChecker
from .manager import NotificationManager
from .models import Notification


class NotificationRouter:

    def __init__(self, config: AgentConfig):
        self.config = config
        self._channel_checker = ChannelStatusChecker(config)
        self._manager = NotificationManager(config)
        self._session_workspace = Path(config.session_workspace)

    def route(self, notification: Notification) -> str | None:
        # 策略 1：尝试发起通道
        if self._channel_checker.check(notification.channel, notification.user_id):
            return notification.channel

        # 策略 2：查询会话活跃通道
        active_channel = self._find_session_active_channel(
            notification.user_id,
            exclude_channel=notification.channel,
        )
        if active_channel and self._channel_checker.check(active_channel, notification.user_id):
            return active_channel

        # 策略 3：查用户所有会话，找任意在线通道
        for channel in self._candidate_fallback_channels(notification.channel):
            if self._channel_checker.check(channel, notification.user_id):
                return channel

        # 策略 4：都不在线，需要存储
        return None

    def _find_session_active_channel(
        self,
        user_id: str,
        exclude_channel: str | None = None,
    ) -> str | None:
        """查找会话最近活跃通道."""
        if not self._session_workspace.exists():
            return None
        timeout = getattr(self.config, "notification_channel_timeout_seconds", 300)
        best_channel: str | None = None
        best_time = 0.0
        try:
            for session_dir in self._session_workspace.iterdir():
                channel, updated_at = self._eval_session_for_router(session_dir, user_id, exclude_channel, timeout)
                best_channel, best_time = _newer_channel(channel, updated_at, best_channel, best_time)
        except OSError:
            return None
        return best_channel

    def _candidate_fallback_channels(self, excluded_channel: str) -> tuple[str, ...]:
        return tuple(channel for channel in ("chat", "feishu", "qq") if channel != excluded_channel)

    def _eval_session_for_router(self, session_dir: Path, user_id: str, exclude_channel: str | None, timeout: float) -> tuple[str | None, float]:
        """Evaluate session dir for active channel; return (channel, updated_at)."""
        if not session_dir.is_dir():
            return None, 0.0
        session_file = session_dir / "session.json"
        if not session_file.exists():
            return None, 0.0
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError):
            return None, 0.0
        if data.get("user_id") != user_id:
            return None, 0.0
        channel = data.get("last_active_channel", "")
        if not channel or channel == exclude_channel:
            return None, 0.0
        updated_at = data.get("updated_at", 0)
        if (time.time() - updated_at) < timeout:
            return channel, updated_at
        return None, 0.0

    def is_channel_online(self, channel: str, user_id: str | None = None) -> bool:
        return self._channel_checker.check(channel, user_id)

    def deliver(self, notification_id: str) -> tuple[bool, str]:
        notification = self._manager.load_notification(notification_id)
        if notification is None:
            return False, f"通知 {notification_id} 不存在"

        if notification.status == "delivered":
            return True, notification.delivery_channel

        # 路由
        target_channel = self.route(notification)

        if target_channel is None:
            # 无法投递，标记为存储
            self._manager.mark_stored(notification_id)
            return False, "无可用通道，已存储"

        # 实际投递（这里是模拟，实际由适配器处理）
        success = self._do_deliver(notification, target_channel)

        if success:
            self._manager.mark_delivered(notification_id, target_channel)
            return True, target_channel
        else:
            self._manager.mark_failed(notification_id, "投递失败")
            return False, "投递失败"

    def _do_deliver(self, notification: Notification, channel: str) -> bool:
        # 模拟投递：实际实现中调用通道适配器
        # 对于 chat 通道，直接输出到 stderr（用于测试）
        # 对于其他通道，调用对应适配器
        if channel == "chat":
            # chat 通道通过 print 输出（实际应该在会话中显示）
            return True
        else:
            # 其他通道调用适配器（这里简化处理）
            return True

    def flush_stored(self, user_id: str) -> int:
        pending = self._manager.get_pending(user_id)
        success_count = 0

        for notification in pending:
            if notification.status != "stored":
                continue
            success, _info = self.deliver(notification.notification_id)
            if success:
                success_count += 1

        return success_count

    def get_pending_count(self, user_id: str) -> int:
        return self._manager.get_pending_count(user_id)


__all__ = ["NotificationRouter"]


def _newer_channel(
    channel: str | None,
    updated_at: float,
    best_channel: str | None,
    best_time: float,
) -> tuple[str | None, float]:
    if updated_at > best_time:
        return channel, updated_at
    return best_channel, best_time
