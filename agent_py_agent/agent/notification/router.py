"""通知路由。

根据用户在线状态和通道可用性，决定通知投递策略：
1. 先尝试发起通道
2. 发起通道不在线 → 转活跃通道
3. 活跃通道也不在线 → 转其他在线通道
4. 都不在线 → 存储等待
"""
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
    """通知路由器。

    决定通知投递策略和实际投递通道。
    """

    def __init__(self, config: AgentConfig):
        """初始化路由器。

        Args:
            config: 智能体配置对象
        """
        self.config = config
        self._channel_checker = ChannelStatusChecker(config)
        self._manager = NotificationManager(config)
        self._session_workspace = Path(config.session_workspace)

    def route(self, notification: Notification) -> str | None:
        """决定投递通道。

        路由策略：
        1. 先尝试发起通道（notification.channel）
        2. 发起通道不在线 → 查 session 的 last_active_channel
        3. 活跃通道也不在线 → 查用户所有会话，找最近的活跃通道
        4. 都不在线 → 返回 None（需要存储）

        Args:
            notification: 通知对象

        Returns:
            实际投递通道，如果需要存储返回 None
        """
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
        for channel in ("chat", "feishu", "qq"):
            if channel != notification.channel:
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
                if updated_at > best_time:
                    best_time = updated_at
                    best_channel = channel
        except OSError:
            return None
        return best_channel

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
        """检查通道是否在线。

        Args:
            channel: 通道名称
            user_id: 用户 ID，可选

        Returns:
            通道是否在线
        """
        return self._channel_checker.check(channel, user_id)

    def deliver(self, notification_id: str) -> tuple[bool, str]:
        """尝试投递通知。

        路由并投递单个通知。

        Args:
            notification_id: 通知 ID

        Returns:
            (是否成功, 投递通道或错误信息)
        """
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
        """执行实际投递。

        实际实现中，这里应该调用对应通道的适配器。
        目前是模拟实现。

        Args:
            notification: 通知对象
            channel: 投递通道

        Returns:
            是否投递成功
        """
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
        """推送用户所有离线存储的通知。

        用户上线时调用，将存储的通知推送给用户。

        Args:
            user_id: 用户 ID

        Returns:
            成功推送的通知数量
        """
        pending = self._manager.get_pending(user_id)
        success_count = 0

        for notification in pending:
            if notification.status == "stored":
                success, info = self.deliver(notification.notification_id)
                if success:
                    success_count += 1

        return success_count

    def get_pending_count(self, user_id: str) -> int:
        """获取用户待处理通知数量。

        Args:
            user_id: 用户 ID

        Returns:
            待处理通知数量
        """
        return self._manager.get_pending_count(user_id)


__all__ = ["NotificationRouter"]
