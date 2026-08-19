# LLM: 本模块定义 chat slash 命令的结构化目录项与执行上下文；帮助、补全和 dispatcher 必须共享这些事实而不是解析展示文案。
# 模块用途: 保存斜杠命令的名称/用法说明，以及命令处理器需要的 agent、配置和输出入口。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


# LLM: SlashCommandSpec 只描述用户可见命令目录；真正控制权限仍由 typed parser/executor 决定，补全结果不能直接执行动作。
# 类用途: 为帮助页和输入补全提供一条命令的固定用法与简短说明。
@dataclass(frozen=True)
class SlashCommandSpec:
    name: str
    usage: str
    summary: str
    submit_on_enter: bool = False


# LLM: SlashCommandContext 汇总 dispatcher 的可信依赖；普通输入文本之外的控制结果只经 control_executor 返回。
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


# LLM: 命令目录属于纯数据，TUI 补全可在不导入 Conversation/Gateway 执行器时读取；执行语义仍由 slash_commands 和 typed parser 拥有。
# 常量用途: 为帮助页、补全菜单和提交行为提供唯一的斜杠命令目录。
CHAT_SLASH_COMMANDS = (
    SlashCommandSpec("help", "/help", "Show help", submit_on_enter=True),
    SlashCommandSpec("status", "/status", "Show the current window status", submit_on_enter=True),
    SlashCommandSpec("context", "/context", "Show real context usage", submit_on_enter=True),
    SlashCommandSpec(
        "compact",
        "/compact [instructions]",
        "Compact completed conversation history now",
        submit_on_enter=True,
    ),
    SlashCommandSpec(
        "effort",
        "/effort [low|medium|high|max|auto]",
        "Show or set model effort when supported",
        submit_on_enter=True,
    ),
    SlashCommandSpec("btw", "/btw <content>", "Steer the current running turn once"),
    SlashCommandSpec("stop", "/stop", "Stop the current running turn", submit_on_enter=True),
    SlashCommandSpec(
        "goal",
        "/goal <duration> <name> <task>",
        "Start a named persistent conversation goal",
    ),
    SlashCommandSpec("goal", "/goal <name> clear", "Stop one named persistent goal"),
    SlashCommandSpec("goal", "/goal pause|resume|clear", "Control the only persistent goal"),
    SlashCommandSpec("goal", "/goal edit <objective>", "Edit the current persistent goal"),
    SlashCommandSpec(
        "verbose",
        "/verbose [off|on|full]",
        "Show or change detailed progress",
        submit_on_enter=True,
    ),
    SlashCommandSpec("audit", "/audit help", "Show Audit command help"),
    SlashCommandSpec("audit", "/audit <name> prepare <text>", "Prepare or revise one named Audit"),
    SlashCommandSpec("audit", "/audit <duration> <name> <task>", "Start one named Audit"),
    SlashCommandSpec("audit", "/audit <name> status", "Show one named Audit"),
    SlashCommandSpec("audit", "/audit <name> clear", "Stop one named Audit"),
    SlashCommandSpec(
        "expand",
        "/expand [last|number]",
        "Expand a collapsed assistant response",
        submit_on_enter=True,
    ),
    SlashCommandSpec("exit", "/exit", "Exit chat", submit_on_enter=True),
    SlashCommandSpec("memory", "/memory [query]", "Search memory", submit_on_enter=True),
    SlashCommandSpec("remember", "/remember <content>", "Save a memory note"),
    SlashCommandSpec("prompt-file", "/prompt-file <path>", "Add a prompt file"),
    SlashCommandSpec("show-prompt", "/show-prompt <question>", "Show the final prompt and answer"),
)


__all__ = ["CHAT_SLASH_COMMANDS", "SlashCommandContext", "SlashCommandSpec"]
