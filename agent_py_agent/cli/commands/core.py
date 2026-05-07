# LLM: CLI command registration module; keep parser wiring and command handler imports stable.
# 模块用途: 组织某组命令行子命令，让用户能从 CLI 触发对应功能。

from __future__ import annotations

"""exposes core command registration through the cli.commands namespace.

给人看的解释：
现有注册逻辑已经按领域拆到 subcommands_* 模块。这里提供新的长期入口，
让 parser.py 只依赖 cli.commands 包，后续可以逐步把实现搬进来。
"""

from ..subcommands_basic import (
    add_basic_subcommands,
    add_local_store_subcommands,
    add_memory_subcommands,
)

__all__ = [
    "add_basic_subcommands",
    "add_local_store_subcommands",
    "add_memory_subcommands",
]

