
from __future__ import annotations

from ..collaboration import (
    cmd_collaboration_list,
    cmd_collaboration_overview,
    cmd_collaboration_status,
    cmd_collaboration_update_request,
    cmd_collaboration_update_status,
)


def add_collaboration_subcommands(subparsers) -> None:
    group = subparsers.add_parser(
        "collaboration",
        help="查看通用多代理协作 case、请求和证据状态",
    )
    sub = group.add_subparsers(dest="collaboration_command")
    _add_overview_command(sub)
    _add_list_command(sub)
    _add_status_command(sub)
    _add_update_request_command(sub)
    _add_update_status_command(sub)


def _add_overview_command(sub) -> None:
    overview_cmd = sub.add_parser("overview", help="查看协作控制面总览和 readiness")
    overview_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    overview_cmd.set_defaults(func=cmd_collaboration_overview)


def _add_list_command(sub) -> None:
    list_cmd = sub.add_parser("list", help="列出协作 case 摘要")
    list_cmd.add_argument("--status", default="", help="可选 case 状态过滤，如 open/escalated")
    list_cmd.add_argument("--limit", type=int, default=20, help="最多返回多少条；0 表示不限制")
    list_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    list_cmd.set_defaults(func=cmd_collaboration_list)


def _add_status_command(sub) -> None:
    status_cmd = sub.add_parser("status", help="查看单个协作 case 详情")
    status_cmd.add_argument("--case-id", required=True, help="协作 case ID")
    status_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    status_cmd.set_defaults(func=cmd_collaboration_status)


def _add_update_status_command(sub) -> None:
    update_cmd = sub.add_parser("update-status", help="推进协作 case 状态并记录决策摘要")
    update_cmd.add_argument("--case-id", required=True, help="协作 case ID")
    update_cmd.add_argument("--status", required=True, help="新状态，如 triaged/resolved/closed 或自定义状态")
    update_cmd.add_argument("--actor-agent-id", default="", help="执行状态推进的代理 ID")
    update_cmd.add_argument("--summary", default="", help="状态推进摘要；关闭/解决类状态必须提供")
    update_cmd.add_argument("--decision-type", default="", help="可选决策类型")
    update_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    update_cmd.set_defaults(func=cmd_collaboration_update_status)


def _add_update_request_command(sub) -> None:
    update_cmd = sub.add_parser("update-request", help="更新协作请求状态")
    update_cmd.add_argument("--case-id", required=True, help="协作 case ID")
    update_cmd.add_argument("--request-id", required=True, help="协作请求 ID")
    update_cmd.add_argument("--status", required=True, help="请求状态，如 working/completed/blocked 或自定义状态")
    update_cmd.add_argument("--actor-agent-id", default="", help="更新请求状态的代理 ID")
    update_cmd.add_argument("--summary", default="", help="请求状态更新摘要")
    update_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    update_cmd.set_defaults(func=cmd_collaboration_update_request)


__all__ = ["add_collaboration_subcommands"]
