# LLM: Home runtime subcommand registration stays separate from the large basic CLI module.
# 模块用途: 注册 owner-home 调试和维护命令，避免主 subcommands_basic 文件继续膨胀。

from __future__ import annotations

import argparse

from .home_runtime_commands import (
    cmd_home_index_rebuild,
    cmd_home_migrate,
    cmd_home_retention,
    cmd_home_status,
    cmd_memory_daily_list,
    cmd_task_workspace_list,
)


# LLM: add_home_runtime_subcommands registers owner-home debug and maintenance commands.
# 函数用途: 注册 home-status、home-migrate、home-retention、home-index-rebuild 和读取侧调试命令。
def add_home_runtime_subcommands(sub: argparse._SubParsersAction) -> None:
    home_status = sub.add_parser("home-status", help="查看 my-agent 家目录入口文件和关键目录")
    home_status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    home_status.set_defaults(func=cmd_home_status)

    home_migrate = sub.add_parser("home-migrate", help="预览或执行旧 home 数据到 owner home 的非破坏性迁移")
    home_migrate.add_argument("--apply", action="store_true", help="实际复制；不传时只预览")
    home_migrate.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    home_migrate.set_defaults(func=cmd_home_migrate)

    home_retention = sub.add_parser("home-retention", help="预览或执行当前 owner home 的 retention 清理")
    home_retention.add_argument("--apply", action="store_true", help="实际删除过期文件；不传时只预览")
    home_retention.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    home_retention.set_defaults(func=cmd_home_retention)

    home_index_rebuild = sub.add_parser("home-index-rebuild", help="预览或重建 owner/task/run/agent 全局索引")
    home_index_rebuild.add_argument("--apply", action="store_true", help="实际追加新索引行；不传时只预览")
    home_index_rebuild.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    home_index_rebuild.set_defaults(func=cmd_home_index_rebuild)

    memory_daily_list = sub.add_parser("memory-daily-list", help="列出 home/memory/daily 按天记忆")
    memory_daily_list.add_argument("query", nargs="?", default="", help="搜索关键词；为空时列出匹配日期的记录")
    memory_daily_list.add_argument("--date", help="只查看某一天，格式 YYYY-MM-DD")
    memory_daily_list.add_argument("--role", default="", help="按 role 过滤，如 user/assistant/tool")
    memory_daily_list.add_argument("--kind", default="", help="按 kind 过滤，如 dialogue/preference/note")
    memory_daily_list.add_argument("--limit", type=int, default=None, help="最多显示多少条记录；默认读配置")
    memory_daily_list.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_daily_list.set_defaults(func=cmd_memory_daily_list)

    task_workspace_list = sub.add_parser("task-workspace-list", help="列出 home workspace/tasks 任务工作区")
    task_workspace_list.add_argument("query", nargs="?", default="", help="搜索 task_id/run_id/request_id/任务名")
    task_workspace_list.add_argument("--date", help="只查看某一天，格式 YYYY-MM-DD")
    task_workspace_list.add_argument("--limit", type=int, default=None, help="最多显示多少个任务；默认读配置")
    task_workspace_list.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    task_workspace_list.set_defaults(func=cmd_task_workspace_list)


__all__ = ["add_home_runtime_subcommands"]
