from __future__ import annotations

"""LLM: builds the argparse root and delegates subcommand registration.

给人看的解释：
这个文件现在只做一件事：创建顶层 parser，然后把各命令族交给
`cli.commands` 下的注册模块。命令参数细节和执行逻辑都不再堆在这里。
"""

import argparse

from .commands.bench import add_bench_model_command
from .commands.core import (
    add_basic_subcommands,
    add_local_store_subcommands,
    add_memory_subcommands,
)
from .commands.gateway import (
    add_adapter_subcommand,
    add_daemon_subcommand,
    add_gateway_subcommands,
    add_logs_subcommands,
    add_scenario_subcommand,
)
from .commands.learning import add_learning_subcommand
from .commands.operations import add_operations_subcommands
from .commands.subagents import add_subagents_subcommands
from .commands.tasks import add_task_subcommands
from .common import DEFAULT_CONFIG, configure_stdio
from .gateway_client import cmd_default


def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog="my-agent",
        description="Simple Python3 CLI Agent with memory, dynamic prompt and subagents.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径，默认使用 config/agent_config.yaml",
    )
    parser.add_argument(
        "--app",
        action="store_true",
        help="启动应用内聊天界面，固定状态栏和输入行，并使用应用内滚动历史",
    )
    subparsers = parser.add_subparsers(dest="command")
    parser.set_defaults(func=cmd_default)

    add_basic_subcommands(subparsers)
    add_bench_model_command(subparsers)
    add_memory_subcommands(subparsers)
    add_local_store_subcommands(subparsers)
    add_logs_subcommands(subparsers)
    add_learning_subcommand(subparsers)
    add_subagents_subcommands(subparsers)
    add_daemon_subcommand(subparsers)
    add_scenario_subcommand(subparsers)
    add_gateway_subcommands(subparsers)
    add_adapter_subcommand(subparsers)
    add_task_subcommands(subparsers)
    add_operations_subcommands(subparsers)

    return parser


def main() -> int:

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "app", False):
        args.app_scrollback = True
    return args.func(args)
