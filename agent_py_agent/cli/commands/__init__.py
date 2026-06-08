
from __future__ import annotations

"""Register small CLI command groups.

These commands are thin argparse definitions whose execution lives in their
domain modules. Keeping them together avoids one-file-per-subcommand facades.
"""

import argparse

from ..audit_log_cmd import cmd_audit_log
from ..bench_model import cmd_bench_model
from ..collaboration import (
    cmd_collaboration_list,
    cmd_collaboration_overview,
    cmd_collaboration_status,
    cmd_collaboration_update_request,
    cmd_collaboration_update_status,
)
from ..guidance_commands import cmd_guidance_send
from ..learning import cmd_learn_accept, cmd_learn_list, cmd_learn_reject, cmd_learn_stats
from ..notifications_cmd import cmd_notifications
from ..task_commands import (
    cmd_task_abandon,
    cmd_task_list,
    cmd_task_pause,
    cmd_task_resume,
    cmd_task_search,
    cmd_task_show,
)


def add_bench_model_command(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    bench = subparsers.add_parser("bench-model", help="运行模型速度基准测试或查看已有速度模型")
    bench.add_argument("--show", action="store_true", help="查看已有速度模型，不运行测试")
    bench.add_argument("--profile", help="速度模型文件路径；默认使用 data/model_speed_profile.json")
    bench.set_defaults(func=cmd_bench_model)


def add_guidance_subcommand(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("guidance-send", help="给运行中的主代理/子代理追加一条软提示")
    parser.add_argument("message", help="要追加的自然语言提示")
    parser.add_argument("--run-id", default="", help="目标代理 run_id")
    parser.add_argument("--thread-id", default="", help="目标会话 thread_id")
    parser.add_argument("--task-id", default="", help="目标任务 task_id")
    parser.add_argument("--case-id", default="", help="目标协作 case_id")
    parser.add_argument("--target-type", default="", help="开放目标类型；常见 agent_run/thread/task/case")
    parser.add_argument("--target-id", default="", help="开放目标 id")
    parser.add_argument("--sender", default="cli_user", help="发送者标记")
    parser.add_argument("--priority", default="normal", help="软优先级")
    parser.add_argument("--delivery", default="next_turn", help="投递提示，默认下一轮读取")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.set_defaults(func=cmd_guidance_send)


def add_collaboration_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = subparsers.add_parser(
        "collaboration",
        help="查看通用多代理协作 case、请求和证据状态",
    )
    sub = group.add_subparsers(dest="collaboration_command")

    overview_cmd = sub.add_parser("overview", help="查看协作控制面总览和 readiness")
    overview_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    overview_cmd.set_defaults(func=cmd_collaboration_overview)

    list_cmd = sub.add_parser("list", help="列出协作 case 摘要")
    list_cmd.add_argument("--status", default="", help="可选 case 状态过滤：open 或 closed")
    list_cmd.add_argument("--limit", type=int, default=20, help="最多返回多少条；0 表示不限制")
    list_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    list_cmd.set_defaults(func=cmd_collaboration_list)

    status_cmd = sub.add_parser("status", help="查看单个协作 case 详情")
    status_cmd.add_argument("--case-id", required=True, help="协作 case ID")
    status_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    status_cmd.set_defaults(func=cmd_collaboration_status)

    update_request_cmd = sub.add_parser("update-request", help="更新协作请求状态")
    update_request_cmd.add_argument("--case-id", required=True, help="协作 case ID")
    update_request_cmd.add_argument("--request-id", required=True, help="协作请求 ID")
    update_request_cmd.add_argument("--status", required=True, help="请求状态：open/pending/completed/blocked/timeout/declined")
    update_request_cmd.add_argument("--actor-agent-id", default="", help="更新请求状态的代理 ID")
    update_request_cmd.add_argument("--summary", default="", help="请求状态更新摘要")
    update_request_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    update_request_cmd.set_defaults(func=cmd_collaboration_update_request)

    update_status_cmd = sub.add_parser("update-status", help="推进协作 case 状态并记录决策摘要")
    update_status_cmd.add_argument("--case-id", required=True, help="协作 case ID")
    update_status_cmd.add_argument("--status", required=True, help="新状态：open 或 closed")
    update_status_cmd.add_argument("--actor-agent-id", default="", help="执行状态推进的代理 ID")
    update_status_cmd.add_argument("--summary", default="", help="状态推进摘要；closed 状态必须提供")
    update_status_cmd.add_argument("--decision-type", default="", help="可选决策类型")
    update_status_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    update_status_cmd.set_defaults(func=cmd_collaboration_update_status)


def add_learning_subcommand(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    learn = subparsers.add_parser("learn", help="管理自动生成的 learning draft 候选")
    learn_sub = learn.add_subparsers(dest="learn_command", required=True)

    learn_list = learn_sub.add_parser("list", help="列出当前 learning draft 候选")
    learn_list.set_defaults(func=cmd_learn_list)

    learn_accept = learn_sub.add_parser("accept", help="确认一个 learning draft")
    learn_accept.add_argument("candidate_id", help="learning draft ID")
    learn_accept.set_defaults(func=cmd_learn_accept)

    learn_reject = learn_sub.add_parser("reject", help="拒绝一个 learning draft")
    learn_reject.add_argument("candidate_id", help="learning draft ID")
    learn_reject.set_defaults(func=cmd_learn_reject)

    learn_stats = learn_sub.add_parser("stats", help="查看 learning draft 汇总统计")
    learn_stats.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    learn_stats.set_defaults(func=cmd_learn_stats)


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


def add_task_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    task_show = subparsers.add_parser("task-show", help="显示任务详情")
    task_show.add_argument("task_id", help="任务 ID")
    task_show.set_defaults(func=cmd_task_show)

    task_list = subparsers.add_parser("task-list", help="列出任务列表")
    task_list.add_argument("--user-id", help="按用户 ID 过滤")
    task_list.add_argument("--status", help="按状态过滤，如 PLANNING/RUNNING/DONE")
    task_list.add_argument("--limit", type=int, default=None, help="最多显示多少条；默认读配置")
    task_list.set_defaults(func=cmd_task_list)

    task_abandon = subparsers.add_parser("task-abandon", help="标记任务为 ABANDONED（不再重试）")
    task_abandon.add_argument("task_id", help="任务 ID")
    task_abandon.set_defaults(func=cmd_task_abandon)

    task_pause = subparsers.add_parser("task-pause", help="暂停任务（后续可 resume）")
    task_pause.add_argument("task_id", help="任务 ID")
    task_pause.set_defaults(func=cmd_task_pause)

    task_resume = subparsers.add_parser("task-resume", help="恢复 PAUSED 任务为 RUNNING")
    task_resume.add_argument("task_id", help="任务 ID")
    task_resume.set_defaults(func=cmd_task_resume)

    task_search = subparsers.add_parser("task-search", help="模糊搜索匹配的任务")
    task_search.add_argument("query", help="搜索关键词或描述")
    task_search.set_defaults(func=cmd_task_search)


__all__ = [
    "add_bench_model_command",
    "add_collaboration_subcommands",
    "add_guidance_subcommand",
    "add_learning_subcommand",
    "add_operations_subcommands",
    "add_task_subcommands",
]
