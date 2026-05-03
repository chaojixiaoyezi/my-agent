"""通知 CLI 命令。

提供 notifications 命令实现：
- 列出用户的通知
- 推送离线存储的通知
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..agent.notification import NotificationManager, NotificationRouter


def cmd_notifications(args) -> int:
    """查看未读通知。

    Args:
        args: 解析后的命令行参数

    Returns:
        退出码
    """
    from .common import DEFAULT_CONFIG, load_config, resolve_workspace_root

    config_path = getattr(args, "config", str(DEFAULT_CONFIG))
    config = load_config(config_path)

    root = resolve_workspace_root(config, config_path)

    manager = NotificationManager(config)
    router = NotificationRouter(config)

    user_id = config.user_id

    # 处理 flush
    if getattr(args, "flush", False):
        count = router.flush_stored(user_id)
        print(f"已推送 {count} 条离线存储的通知。", file=sys.stdout)
        return 0

    # 列出通知
    include_all = getattr(args, "all", False)
    limit = getattr(args, "limit", 20)

    notifications = manager.list_notifications(
        user_id,
        limit=limit,
        include_delivered=include_all,
    )

    if not notifications:
        print("暂无通知。", file=sys.stdout)
        return 0

    # 格式化输出
    pending_count = manager.get_pending_count(user_id)
    if pending_count > 0 and not include_all:
        print(f"你有 {pending_count} 条未读通知：", file=sys.stdout)
        print()

    for n in notifications:
        status_icon = {
            "pending": "⏳",
            "delivered": "✓",
            "failed": "✗",
            "stored": "📦",
        }.get(n.status, "?")

        print(f"{status_icon} [{n.status.upper()}] {n.notification_id}", file=sys.stdout)
        print(f"  任务: {n.task_id}", file=sys.stdout)
        print(f"  消息: {n.message}", file=sys.stdout)
        print(f"  发起通道: {n.channel}", file=sys.stdout)

        if n.status == "delivered":
            print(f"  投递通道: {n.delivery_channel}", file=sys.stdout)
            print(f"  投递时间: {n.delivered_at}", file=sys.stdout)

        print()

    return 0


__all__ = ["cmd_notifications"]
