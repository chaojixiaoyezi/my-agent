
from __future__ import annotations

"""subcommand registration helpers – re-export module.

给人看的解释：
这个文件原来是所有子命令注册函数的单一大文件，现已按领域拆分为：
  - subcommands_basic.py     基础 / 记忆 / 本地事实源子命令
  - subcommands_gateway.py   日志 / gateway / 适配器 / 守护进程 / 场景测试子命令
  - subcommands_agents.py    子代理相关子命令

本文件保留为薄 re-export 层，下游只需 import 这里即可，无需知道拆分细节。
"""

from .subcommands_agents import add_subagents_subcommands
from .subcommands_basic import (
    add_basic_subcommands,
    add_local_store_subcommands,
    add_memory_subcommands,
)
from .subcommands_gateway import (
    add_adapter_subcommand,
    add_daemon_subcommand,
    add_gateway_subcommands,
    add_logs_subcommands,
    add_scenario_subcommand,
)

__all__ = [
    "add_adapter_subcommand",
    "add_basic_subcommands",
    "add_daemon_subcommand",
    "add_gateway_subcommands",
    "add_local_store_subcommands",
    "add_logs_subcommands",
    "add_memory_subcommands",
    "add_scenario_subcommand",
    "add_subagents_subcommands",
]
