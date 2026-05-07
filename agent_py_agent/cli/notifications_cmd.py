# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..agent.notification import NotificationManager, NotificationRouter


# LLM: _format_notification 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _format_notification(n, include_delivery: bool = False) -> str:
    status_icon = {
        "pending": "⏳",
        "delivered": "✓",
        "failed": "✗",
        "stored": "📦",
    }.get(n.status, "?")

    lines = [
        f"{status_icon} [{n.status.upper()}] {n.notification_id}",
        f"  任务: {n.task_id}",
        f"  消息: {n.message}",
        f"  发起通道: {n.channel}",
    ]

    if include_delivery and n.status == "delivered":
        lines.append(f"  投递通道: {n.delivery_channel}")
        lines.append(f"  投递时间: {n.delivered_at}")

    return "\n".join(lines)


# LLM: cmd_notifications 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_notifications(args) -> int:
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
        print(_format_notification(n, include_delivery=True), file=sys.stdout)
        print()

    return 0


__all__ = ["cmd_notifications"]
