
from __future__ import annotations

"""builds the argparse root and delegates subcommand registration.

给人看的解释：
这个文件现在只做一件事：创建顶层 parser，然后把各命令族交给
`cli.commands` 下的注册模块。命令参数细节和执行逻辑都不再堆在这里。
"""

import argparse

from ..agent.extensions import ExtensionRegistry, load_extension_registry
from ..agent.settings import load_config
from .background_main_agent import add_background_main_agent_subcommands
from .commands import (
    add_bench_model_command,
    add_collaboration_subcommands,
    add_guidance_subcommand,
    add_learning_subcommand,
    add_operations_subcommands,
    add_task_subcommands,
)
from .common import DEFAULT_CONFIG, configure_stdio
from .config_cmd import add_config_subcommands
from .contracts_commands import add_contracts_subcommand
from .feishu_cmd import add_feishu_subcommands
from .gateway_client import cmd_default
from .subagents import add_subagents_subcommands
from .subcommands_basic import (
    add_basic_subcommands,
    add_local_store_subcommands,
    add_memory_subcommands,
)
from .subcommands_gateway import (
    add_adapter_subcommand,
    add_daemon_subcommand,
    add_gateway_subcommands,
    add_scenario_subcommand,
)
from .update import add_update_subcommand


def build_parser(extension_registry: ExtensionRegistry | None = None) -> argparse.ArgumentParser:

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
    add_learning_subcommand(subparsers)
    add_guidance_subcommand(subparsers)
    add_subagents_subcommands(subparsers)
    add_daemon_subcommand(subparsers)
    add_scenario_subcommand(subparsers)
    add_gateway_subcommands(subparsers)
    add_adapter_subcommand(subparsers)
    add_task_subcommands(subparsers)
    add_operations_subcommands(subparsers)
    add_background_main_agent_subcommands(subparsers)
    add_collaboration_subcommands(subparsers)
    add_update_subcommand(subparsers)
    add_config_subcommands(subparsers)
    add_feishu_subcommands(subparsers)
    if extension_registry is not None:
        extension_registry.register_cli_commands(subparsers)

    return parser


def main() -> int:

    configure_stdio()
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", default=str(DEFAULT_CONFIG))
    bootstrap_args, _ = bootstrap.parse_known_args()
    config = load_config(bootstrap_args.config)
    extension_registry = load_extension_registry(config.extension_plugins)
    parser = build_parser(extension_registry)
    args = parser.parse_args()
    args.app_scrollback = not bool(getattr(args, "plain", False))
    return args.func(args)
