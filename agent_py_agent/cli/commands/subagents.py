from __future__ import annotations

"""LLM: exposes subagent command registration through cli.commands.

给人看的解释：
子代理 CLI 命令很多，parser.py 不应该直接承载这些参数细节。
"""

from ..subcommands_agents import add_subagents_subcommands

__all__ = ["add_subagents_subcommands"]

