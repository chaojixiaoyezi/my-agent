
from __future__ import annotations

"""registers operational inspection commands.

给人看的解释：
这些命令用于通知和审计查询，属于运维/观察入口，不放在业务状态机里。
"""

import argparse

from ..audit_log_cmd import cmd_audit_log
from ..notifications_cmd import cmd_notifications


def add_operations_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:

    notifications = subparsers.add_parser("notifications", help="查看未读通知")
    notifications.add_argument("--all", action="store_true", help="列出所有通知（含已读）")
    notifications.add_argument("--flush", action="store_true", help="推送所有离线存储的通知")
    notifications.add_argument("--limit", type=int, default=None, help="最多显示多少条；默认读配置")
    notifications.set_defaults(func=cmd_notifications)

    audit = subparsers.add_parser("audit-log", help="查询审计日志")
    audit.add_argument("--user", help="按用户 ID 过滤")
    audit.add_argument("--action", help="按动作类型过滤")
    audit.add_argument("--target", help="按目标 ID 过滤")
    audit.add_argument("--target-type", help="按目标类型过滤")
    audit.add_argument("--status", help="按状态过滤（success/denied/error）")
    audit.add_argument("--limit", type=int, default=None, help="最多显示多少条；默认读配置")
    audit.add_argument("--offset", type=int, default=0, help="跳过多少条（用于分页）")
    audit.add_argument("--recent-users", action="store_true", help="显示最近活跃用户")
    audit.add_argument("--summary", action="store_true", help="显示统计摘要")
    audit.add_argument("--cleanup", action="store_true", help="清理旧审计记录")
    audit.add_argument("--days", type=int, default=None, help="清理时保留天数；默认读配置")
    audit.set_defaults(func=cmd_audit_log)
