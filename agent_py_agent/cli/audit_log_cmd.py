"""审计日志 CLI 命令。

提供 audit-log 命令实现：
- 按用户、动作、目标过滤查询
- 显示最近活跃用户
- 清理旧审计条目
"""
from __future__ import annotations

import json
import sys
import time

from ..agent.audit import AuditAction, AuditQuery


def _show_recent_users(query: AuditQuery, args) -> int:
    """Show recent active users."""
    limit = getattr(args, "limit", 10)
    users = query.recent_users(limit=limit)

    if not users:
        print("暂无活跃用户。", file=sys.stdout)
        return 0

    print(f"最近活跃用户（共 {len(users)} 人）：", file=sys.stdout)
    print()

    for i, u in enumerate(users, 1):
        last_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(u["last_action_time"]))
        print(f"{i}. {u['user_id']}", file=sys.stdout)
        print(f"   最后活动: {last_time}", file=sys.stdout)
        print()

    return 0


def _show_summary(query: AuditQuery, args) -> int:
    """Show audit summary statistics."""
    user_id = getattr(args, "user", None)
    stats = query.summary(user_id=user_id)

    print(f"审计统计（用户: {user_id or '全部'}）：", file=sys.stdout)
    print(f"  总操作数: {stats['total_actions']}", file=sys.stdout)

    if stats["by_action"]:
        print("  按动作类型：", file=sys.stdout)
        for action, count in sorted(stats["by_action"].items(), key=lambda x: -x[1]):
            print(f"    {action}: {count}", file=sys.stdout)

    if stats["by_status"]:
        print("  按状态：", file=sys.stdout)
        for status, count in sorted(stats["by_status"].items()):
            print(f"    {status}: {count}", file=sys.stdout)

    if stats["last_action_time"]:
        last_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stats["last_action_time"]))
        print(f"  最后操作: {last_str}", file=sys.stdout)

    return 0


def _show_entries(query: AuditQuery, args) -> int:
    """Show audit log entries."""
    user_id = getattr(args, "user", None)
    action_str = getattr(args, "action", None)
    target_id = getattr(args, "target", None)
    target_type = getattr(args, "target_type", None)
    status = getattr(args, "status", None)
    limit = getattr(args, "limit", 100)
    offset = getattr(args, "offset", 0)

    # 转换动作字符串为 AuditAction
    action = None
    if action_str:
        try:
            action = AuditAction(action_str)
        except ValueError:
            pass

    result = query.query(
        user_id=user_id,
        action=action,
        target_id=target_id,
        target_type=target_type,
        status=status,
        limit=limit,
        offset=offset,
    )

    if not result.entries:
        print("没有匹配的审计记录。", file=sys.stdout)
        return 0

    print(f"找到 {result.total_count} 条审计记录（显示 {len(result.entries)} 条）：", file=sys.stdout)
    print(f"查询耗时: {result.query_time_ms:.2f}ms", file=sys.stdout)
    print()

    for entry in result.entries:
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.timestamp))

        print(f"[{entry.status.upper()}] {entry.action} - {entry.user_id}", file=sys.stdout)
        print(f"  时间: {time_str}", file=sys.stdout)
        print(f"  通道: {entry.channel}", file=sys.stdout)
        print(f"  目标: {entry.target_type}/{entry.target_id}", file=sys.stdout)

        if entry.ip_address:
            print(f"  IP: {entry.ip_address}", file=sys.stdout)

        if entry.details:
            print(f"  详情: {json.dumps(entry.details, ensure_ascii=False)}", file=sys.stdout)

        print()

    return 0


def cmd_audit_log(args) -> int:
    """查询审计日志。

    Args:
        args: 解析后的命令行参数

    Returns:
        退出码
    """
    from .common import DEFAULT_CONFIG, load_config, resolve_workspace_root

    config_path = getattr(args, "config", str(DEFAULT_CONFIG))
    config = load_config(config_path)

    root = resolve_workspace_root(config, config_path)

    query = AuditQuery(config)

    # 最近活跃用户
    if getattr(args, "recent_users", False):
        return _show_recent_users(query, args)

    # 摘要统计
    if getattr(args, "summary", False):
        return _show_summary(query, args)

    # 清理旧条目
    if getattr(args, "cleanup", False):
        days = getattr(args, "days", 90)
        count = query.cleanup_old_entries(days=days)
        print(f"已清理 {count} 条超过 {days} 天的审计记录。", file=sys.stdout)
        return 0

    # 查询日志
    return _show_entries(query, args)


__all__ = ["cmd_audit_log"]