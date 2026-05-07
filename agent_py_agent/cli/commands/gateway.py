# LLM: CLI command registration module; keep parser wiring and command handler imports stable.
# 模块用途: 组织某组命令行子命令，让用户能从 CLI 触发对应功能。

from __future__ import annotations

"""exposes gateway-side command registration through cli.commands.

给人看的解释：
gateway、adapter、daemon、logs 和 scenario 都是运行时/外部入口相关命令，
先通过这个模块统一接入薄 parser。
"""

from ..subcommands_gateway import (
    add_adapter_subcommand,
    add_daemon_subcommand,
    add_gateway_subcommands,
    add_logs_subcommands,
    add_scenario_subcommand,
)

__all__ = [
    "add_adapter_subcommand",
    "add_daemon_subcommand",
    "add_gateway_subcommands",
    "add_logs_subcommands",
    "add_scenario_subcommand",
]

