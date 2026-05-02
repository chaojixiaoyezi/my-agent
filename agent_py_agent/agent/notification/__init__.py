"""通知模块。

提供任务完成通知推送能力：
- 通知模型和数据结构
- 通知管理器
- 通知路由器
- 通道状态检测
"""
from __future__ import annotations

from .channel_status import ChannelStatusChecker
from .manager import NotificationManager
from .models import Notification, NotificationDelivery, generate_notification_id
from .router import NotificationRouter

__all__ = [
    "Notification",
    "NotificationDelivery",
    "generate_notification_id",
    "NotificationManager",
    "NotificationRouter",
    "ChannelStatusChecker",
]
