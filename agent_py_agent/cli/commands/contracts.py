# LLM: CLI command registration module; keep contract command wiring stable.
# 模块用途: 把 contracts 子命令注册到 parser.py，具体逻辑留在 contracts_commands.py。

from __future__ import annotations

from ..contracts_commands import add_contracts_subcommand

__all__ = ["add_contracts_subcommand"]
