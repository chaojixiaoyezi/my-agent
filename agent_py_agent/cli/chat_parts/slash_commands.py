# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

from collections.abc import Callable

from ...agent.agent_core.subagent_params import SpawnSubagentsParams
from .slash_command_types import SlashCommandContext

CHAT_HELP_TEXT = (
    "Available commands:\n"
    "/help                         Show help\n"
    "/status                       Show background task status\n"
    "/expand [last|number]          Expand a collapsed assistant response\n"
    "/exit                         Exit chat\n"
    "/memory [query]                Search memory\n"
    "/remember <content>            Save a memory note\n"
    "/btw                          Show runtime prompt injections\n"
    "/btw <content>                 Add a runtime prompt injection\n"
    "/btw-clear                    Clear runtime prompt injections\n"
    "/prompt-file <path>            Add a prompt file\n"
    "/subagents <count> <goal>      Spawn subagent task records\n"
    "/show-prompt <question>        Show the final prompt and answer\n"
)


SlashHandler = Callable[[str, SlashCommandContext, bool], bool | None]


# LLM: handle_common_slash_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def handle_common_slash_command(
    user: str,
    *,
    ctx: SlashCommandContext,
    include_fallback_help: bool = False,
) -> bool:
    handlers: tuple[SlashHandler, ...] = (
        _handle_help_command,
        _handle_remember_command,
        _handle_memory_command,
        _handle_btw_command,
        _handle_prompt_file_command,
        _handle_subagents_command,
    )
    for handler in handlers:
        result = handler(user, ctx, include_fallback_help)
        if result is not None:
            return result
    return False


# LLM: _handle_help_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_help_command(
    user: str, ctx: SlashCommandContext, include_fallback_help: bool
) -> bool | None:
    if user != "/help":
        return None
    suffix = "Ctrl+C                        Exit\nOther input                   Send a normal message\n"
    ctx.print_line(CHAT_HELP_TEXT + (suffix if include_fallback_help else ""))
    return True


# LLM: _handle_remember_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_remember_command(
    user: str, ctx: SlashCommandContext, include_fallback_help: bool
) -> bool | None:
    del include_fallback_help
    if not user.startswith("/remember "):
        return None
    rec = ctx.agent.remember(user[len("/remember "):], kind="note")
    ctx.print_line(f"Remembered: {rec.content}")
    return True


# LLM: _handle_memory_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_memory_command(
    user: str, ctx: SlashCommandContext, include_fallback_help: bool
) -> bool | None:
    del include_fallback_help
    if not user.startswith("/memory"):
        return None
    query = user[len("/memory"):].strip()
    records = ctx.agent.recall(query, ctx.memory_limit) if query else _recent_memory(ctx)
    _print_memory_records(ctx, records)
    return True


# LLM: _recent_memory 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _recent_memory(ctx: SlashCommandContext):
    return ctx.agent.memory.all()[-ctx.memory_limit:]


# LLM: _print_memory_records 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_memory_records(ctx: SlashCommandContext, records) -> None:
    if not records:
        ctx.print_line("No memory records found.")
        return
    for rec in records:
        ctx.print_line(f"- [{rec.kind}] {rec.role}: {rec.content}")


# LLM: _handle_btw_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_btw_command(
    user: str, ctx: SlashCommandContext, include_fallback_help: bool
) -> bool | None:
    del include_fallback_help
    if user == "/btw":
        _print_runtime_injections(ctx)
        return True
    if user.startswith("/btw "):
        ctx.runtime_inject.append(user[len("/btw "):])
        ctx.print_line(f"Added runtime prompt injection; count={len(ctx.runtime_inject)}.")
        return True
    if user == "/btw-clear":
        ctx.runtime_inject.clear()
        ctx.print_line("Cleared runtime prompt injections.")
        return True
    return None


# LLM: _print_runtime_injections 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_runtime_injections(ctx: SlashCommandContext) -> None:
    if not ctx.runtime_inject:
        ctx.print_line("No runtime prompt injections.")
        return
    ctx.print_line("Runtime prompt injections:")
    for index, item in enumerate(ctx.runtime_inject, 1):
        ctx.print_line(f"{index}. {item}")


# LLM: _handle_prompt_file_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_prompt_file_command(
    user: str, ctx: SlashCommandContext, include_fallback_help: bool
) -> bool | None:
    del include_fallback_help
    if not user.startswith("/prompt-file "):
        return None
    ctx.prompt_files.append(user[len("/prompt-file "):].strip())
    ctx.print_line(f"Added prompt file; count={len(ctx.prompt_files)}.")
    return True


# LLM: _handle_subagents_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_subagents_command(
    user: str, ctx: SlashCommandContext, include_fallback_help: bool
) -> bool | None:
    del include_fallback_help
    if not user.startswith("/subagents "):
        return None
    parts = user.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        ctx.print_line("Usage: /subagents <count> <goal>")
        return True
    for task in ctx.agent.spawn_subagents(
        params=SpawnSubagentsParams(goal=parts[2], count=int(parts[1])),
    ):
        ctx.print_line(f"- {task.id}: {task.goal}")
    return True


# LLM: is_exit_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def is_exit_command(user: str) -> bool:
    return user.lower() in {"/exit", "/logout", "/quit", "exit", "logout", "退出"}


# LLM: parse_expand_target 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def parse_expand_target(raw: str) -> str | None:
    if raw == "/expand":
        return "last"
    target = raw[len("/expand "):].strip()
    if not target:
        return "last"
    if target.isdigit():
        return target
    return None


# LLM: is_show_prompt_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def is_show_prompt_command(user: str) -> tuple[bool, str]:
    if user.startswith("/show-prompt "):
        return True, user[len("/show-prompt "):]
    return False, user


__all__ = [
    "CHAT_HELP_TEXT",
    "handle_common_slash_command",
    "is_exit_command",
    "is_show_prompt_command",
    "parse_expand_target",
]
