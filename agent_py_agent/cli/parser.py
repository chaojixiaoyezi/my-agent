
from __future__ import annotations

# LLM: The module import itself is a startup boundary. Keep module-scope imports dependency-light;
# full command families and extension discovery are loaded only when their route is actually needed.
# 模块用途: 构建命令行解析器并分发命令；chat/resume 走轻量入口，让 TUI 不必等待所有管理
# 命令和插件加载，其他命令仍使用完整解析器。

"""builds the argparse root and delegates subcommand registration.

给人看的解释：
这个文件现在只做一件事：创建顶层 parser，然后把各命令族交给
`cli.commands` 下的注册模块。命令参数细节和执行逻辑都不再堆在这里。
"""

import argparse
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .argv_compat_v1 import CanonicalArgumentParser, CanonicalCommandRegistrar
from .bootstrap import configure_stdio, default_config_path
from .chat_command_parser import add_chat_subcommands, add_workspace_arguments

if TYPE_CHECKING:
    from ..agent.extensions import ExtensionRegistry


# LLM: 全量解析器在原run注册后补共用目录参数，顶层与子命令均走同一工作区字段，不改其它命令处理器。
# Full parser compatibility remains the authority for every non-interactive command. Keep
# registrations unchanged and pass an already loaded extension registry from main().
# `skills` is the only user entry that may confirm self-learning Skill proposals.
# `admin-password` / `admin-identities` are the only CLI entries for the IM admin password and bindings.
# 函数用途: 构建包含全部内置命令和扩展命令的解析器，供管理命令、测试和帮助页使用。
def build_parser(extension_registry: ExtensionRegistry | None = None) -> argparse.ArgumentParser:
    parser = _build_root_parser()
    parser.add_subparsers(dest="command")
    subparsers = CanonicalCommandRegistrar(parser)

    from .admin_identity_commands import add_admin_identity_subcommands
    from .background_main_agent import add_background_main_agent_subcommands
    from .commands import (
        add_bench_model_command,
        add_collaboration_subcommands,
        add_guidance_subcommand,
        add_operations_subcommands,
        add_task_subcommands,
    )
    from .config_cmd import add_config_subcommands
    from .contracts_commands import add_contracts_subcommand
    from .feishu_cmd import add_feishu_subcommands
    from .gateway_client import cmd_default
    from .skill_proposal_commands import add_skill_subcommands
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

    parser.set_defaults(func=cmd_default)

    add_basic_subcommands(subparsers)
    add_workspace_arguments(subparsers.choices["run"])
    add_bench_model_command(subparsers)
    add_contracts_subcommand(subparsers)
    add_memory_subcommands(subparsers)
    add_skill_subcommands(subparsers)
    add_local_store_subcommands(subparsers)
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
    add_admin_identity_subcommands(subparsers)
    if extension_registry is not None:
        extension_registry.register_cli_commands(subparsers)

    memory = subparsers.choices["memory"]
    memory_actions = next(a for a in memory._actions if isinstance(a, argparse._SubParsersAction))
    doctor = memory_actions.choices["doctor"]
    doctor.add_argument("--compat-view", choices=["v1"], default="", help=argparse.SUPPRESS)
    doctor.add_argument("--scope", choices=["runtime", "memory", "all"], default="memory")
    return parser


# LLM: Fast parser intentionally registers only interactive commands and never loads extensions.
# Extensions can add sibling CLI commands but cannot change the canonical built-in chat contract.
# 函数用途: 构建只含 chat/resume 的轻量解析器，用于尽快进入交互界面。
def build_interactive_parser() -> argparse.ArgumentParser:
    parser = _build_root_parser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_chat_subcommands(subparsers, lazy_handlers=True)
    return parser


# LLM: 顶层参数和显式目录由完整/轻量解析器复用；不得在注册期导入Agent或初始化状态。
# 函数用途: 创建两种解析器共用的顶层参数，保证配置路径和普通终端开关语义一致。
def _build_root_parser() -> argparse.ArgumentParser:
    parser = CanonicalArgumentParser(
        prog="my-agent",
        description="Simple Python3 CLI Agent with memory, dynamic prompt and subagents.",
    )
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="配置文件路径；默认读取 MY_AGENT_CONFIG，未设置时使用随包配置",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="使用普通终端聊天模式，不进入应用内滚动历史界面",
    )
    add_workspace_arguments(parser)
    parser.legacy_input_boundary = True
    return parser


# LLM: 预解析目录别名只为定位命令，不解释路径或签权；裸--dir启动仍走chat。
# This pre-parser only recognizes the default fresh-chat route or explicit built-in
# chat/resume structure. A bare invocation is the same fresh-session contract as `chat`; unknown
# commands still fall through to the full parser without inspecting natural-language content.
# 函数用途: 识别“直接运行 my-agent”以及显式 chat/resume，让普通 TUI 启动不加载全部管理命令。
def _interactive_command(argv: Sequence[str]) -> str:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config")
    bootstrap.add_argument("--plain", action="store_true")
    add_workspace_arguments(bootstrap)
    _, remaining = bootstrap.parse_known_args(list(argv))
    if not remaining:
        return "chat"
    return remaining[0] if remaining[0] in {"chat", "resume"} else ""


# LLM: 与路由预解析共用目录别名；只给无命令启动补chat，保留顶层显式目录字段与位置。
# The interactive parser requires an explicit subcommand. Inject `chat` only when the
# pre-parser proved that no command remains after root options; never rewrite an unknown route.
# 函数用途: 给裸启动补上内部 chat 子命令，同时保留 --config/--plain 等顶层参数的位置和含义。
def _interactive_argv(argv: Sequence[str], command: str) -> list[str]:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config")
    bootstrap.add_argument("--plain", action="store_true")
    add_workspace_arguments(bootstrap)
    _, remaining = bootstrap.parse_known_args(list(argv))
    if remaining or command != "chat":
        return list(argv)
    return [*argv, "chat"]


# LLM: Dispatch interactive commands before extension/config bootstrap; all other routes preserve
# the existing full parser and extension registration behavior. argv injection exists for tests.
#   内部worker须先走原dispatcher核C启动模式，再进入任何配置/扩展引导；普通命令返回None，保原Count0路径。
# 函数用途: 先分流真实worker，再选择普通轻量或完整解析器；不以CLI参数授予worker业务权限。
def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    from .gateway_worker import dispatch_worker_entry

    worker_code = dispatch_worker_entry(effective_argv)
    if worker_code is not None:
        return worker_code
    interactive_command = _interactive_command(effective_argv)
    if interactive_command:
        parser = build_interactive_parser()
        args = parser.parse_args(_interactive_argv(effective_argv, interactive_command))
        args.app_scrollback = not bool(getattr(args, "plain", False))
        return args.func(args)

    from ..agent.extensions import load_extension_registry
    from ..agent.settings import load_config
    from .host_state_guard import host_state_read_only_guard

    # H3：在模型命令的沙箱里宿主状态只读，CLI 起不来时给结构化错误码（只看宿主设的标记和错误码，见 host_state_guard）。
    with host_state_read_only_guard():
        bootstrap = argparse.ArgumentParser(add_help=False)
        bootstrap.add_argument("--config", default=str(default_config_path()))
        bootstrap_args, _ = bootstrap.parse_known_args(effective_argv)
        config = load_config(bootstrap_args.config)
        extension_registry = load_extension_registry(config.extension_plugins)
        parser = build_parser(extension_registry)
        args = parser.parse_args(effective_argv)
        args.app_scrollback = not bool(getattr(args, "plain", False))
        return args.func(args)
