"""LLM: shared dataclass definitions for slash-command handling.

给人看的解释：
TUI 和 fallback 两套聊天循环的斜杠命令共享同一个 SlashCommandContext 类型，
放在这里避免 input_loop.py 和 slash_commands.py 之间的循环导入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class SlashCommandContext:
    """Bundle of context parameters for handle_common_slash_command."""
    agent: Any
    memory_limit: int
    runtime_inject: list[str]
    prompt_files: list[str]
    print_line: Callable[[str], None]
