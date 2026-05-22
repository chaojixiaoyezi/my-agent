# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""builds the argparse root and delegates subcommand registration.

给人看的解释：
这个文件现在只做一件事：创建顶层 parser，然后把各命令族交给
`cli.commands` 下的注册模块。命令参数细节和执行逻辑都不再堆在这里。
"""

import argparse

from .commands.bench import add_bench_model_command
from .commands.contracts import add_contracts_subcommand
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
from .real_e2e_commands import add_real_e2e_subcommand


# LLM: build_parser 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
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
        help="兼容参数：默认已启动应用内聊天界面",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="使用普通终端聊天模式，不进入应用内滚动历史界面",
    )
    subparsers = parser.add_subparsers(dest="command")
    parser.set_defaults(func=cmd_default)

    add_basic_subcommands(subparsers)
    add_bench_model_command(subparsers)
    add_contracts_subcommand(subparsers)
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
    add_real_e2e_subcommand(subparsers)

    return parser


# LLM: main 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 脚本入口，解析参数、运行主流程，并用退出码表达成功或失败。
def main() -> int:

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    args.app_scrollback = not bool(getattr(args, "plain", False))
    return args.func(args)
