# LLM: 此模块只提供 CLI 分派的可信上下文；命令声明与词法事实归公共 command_catalog，权限仍由宿主裁决。
# 模块用途: 把当前聊天处理器所需的 agent、配置和输出入口集中传入，不保存命令目录或执行状态。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .command_interaction import CommandInteraction


# LLM: 控制执行与命令交互分别传入；use_gateway 是原 CLI 模式，plugin_revision 只保存原选择，交互引用不授予权限。
# 类用途: 把 agent、记忆限制、运行注入、提示文件和输出函数交给通用命令处理器。
@dataclass
class SlashCommandContext:
    agent: Any
    memory_limit: int
    runtime_inject: list[str]
    prompt_files: list[str]
    print_line: Callable[[str], None]
    control_executor: Callable[[Any], Any] | None = None
    conversation_id: str = "default"
    use_gateway: bool = False
    plugin_client: Any = None
    plugin_revision: str = ""
    command_interaction: CommandInteraction | None = None


__all__ = ["SlashCommandContext"]
