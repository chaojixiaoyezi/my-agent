# LLM: CLI command registration module; keep parser wiring and command handler imports stable.
# 模块用途: 组织某组命令行子命令，让用户能从 CLI 触发对应功能。

from __future__ import annotations

"""exposes subagent command registration through cli.commands.

给人看的解释：
子代理 CLI 命令很多，parser.py 不应该直接承载这些参数细节。
"""

from ..subcommands_agents import add_subagents_subcommands

__all__ = ["add_subagents_subcommands"]

