"""LLM: implements visible CLI diagnostics for memory routing and archive state.

给人看的解释：
这里放 memory 新骨架的命令行入口，委托给子模块处理具体逻辑。
当前包含 cmd_memory_route 和 cmd_memory_doctor。
"""

from __future__ import annotations

# Re-export commands from submodules for backward compatibility
from .memory_commands import cmd_memory_route, cmd_memory_doctor

__all__ = [
    "cmd_memory_route",
    "cmd_memory_doctor",
]