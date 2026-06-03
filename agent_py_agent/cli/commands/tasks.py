
from __future__ import annotations

"""registers task inspection and lifecycle commands.

给人看的解释：
task-* 命令是任务事实源的外部入口；注册逻辑集中在这里，执行逻辑仍在 task_commands.py。
"""

import argparse

from ..task_commands import (
    cmd_task_abandon,
    cmd_task_list,
    cmd_task_pause,
    cmd_task_resume,
    cmd_task_search,
    cmd_task_show,
)


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
