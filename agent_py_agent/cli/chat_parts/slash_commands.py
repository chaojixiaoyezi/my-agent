
from __future__ import annotations

from collections.abc import Callable

from ...agent.conversation.control_commands import (
    parse_conversation_control,
    parse_conversation_task_command,
    system_slash_command_name,
)
from .slash_command_types import SlashCommandContext

CHAT_HELP_TEXT = (
    "Available commands:\n"
    "/help                         Show help\n"
    "/status                       Show the current window status\n"
    "/btw <content>                Steer the current running turn once\n"
    "/stop                         Stop the current running turn\n"
    "/goal <duration> <name> <task> Start a named persistent conversation goal\n"
    "/goal <name> clear             Stop one named persistent goal\n"
    "/goal pause|resume|clear       Control the only persistent goal\n"
    "/goal edit <objective>        Edit the current persistent goal\n"
    "/verbose [off|on|full]         Show or change detailed progress\n"
    "/audit help                    Show Audit command help\n"
    "/audit <name> prepare <text>   Prepare or revise one named Audit\n"
    "/audit <duration> <name> <task> Start one named Audit\n"
    "/audit <name> status           Show one named Audit\n"
    "/audit <name> clear            Stop one named Audit\n"
    "/expand [last|number]          Expand a collapsed assistant response\n"
    "/exit                         Exit chat\n"
    "/memory [query]                Search memory\n"
    "/remember <content>            Save a memory note\n"
    "/prompt-file <path>            Add a prompt file\n"
    "/show-prompt <question>        Show the final prompt and answer\n"
)


SlashHandler = Callable[[str, SlashCommandContext, bool], bool | None]


def handle_common_slash_command(
    user: str,
    *,
    ctx: SlashCommandContext,
    include_plain_help: bool = False,
) -> bool:
    handlers: tuple[SlashHandler, ...] = (
        _handle_help_command,
        _handle_control_command,
        _handle_remember_command,
        _handle_memory_command,
        _handle_prompt_file_command,
        _handle_audit_command,
        _handle_unsupported_slash_command,
    )
    for handler in handlers:
        result = handler(user, ctx, include_plain_help)
        if result is not None:
            return result
    return False


# LLM: Local slash parsing delegates to the adapter-neutral typed control protocol.
# 函数用途：即时处理状态、单次纠偏和停止命令，不把它们排进普通聊天任务。
def _handle_control_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    command = parse_conversation_control(user)
    if command is None:
        return None
    if not command.valid:
        ctx.print_line(command.usage)
        return True
    if ctx.control_executor is None:
        ctx.print_line("当前聊天界面没有可用的任务控制入口。")
        return True
    result = ctx.control_executor(command)
    if command.kind == "stop":
        # `/stop` is the text equivalent of the UI stop button.  The control
        # result remains available to programmatic callers, but the chat UI
        # must not turn the button press into a second assistant-style message.
        return True
    ctx.print_line(str(getattr(result, "message", "") or "控制命令没有返回结果。"))
    return True


def _handle_help_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    if user != "/help":
        return None
    suffix = "Ctrl+C                        Exit\nOther input                   Send a normal message\n"
    ctx.print_line(CHAT_HELP_TEXT + (suffix if include_plain_help else ""))
    return True


def _handle_remember_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/remember "):
        return None
    rec = ctx.agent.remember(user[len("/remember "):], kind="fact")
    ctx.print_line(f"Remembered: {rec.content}")
    return True


def _handle_memory_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/memory"):
        return None
    query = user[len("/memory"):].strip()
    records = ctx.agent.recall(query, ctx.memory_limit) if query else _recent_memory(ctx)
    _print_memory_records(ctx, records)
    return True


def _recent_memory(ctx: SlashCommandContext):
    return ctx.agent.memory.all()[-ctx.memory_limit:]


def _print_memory_records(ctx: SlashCommandContext, records) -> None:
    if not records:
        ctx.print_line("No memory records found.")
        return
    for rec in records:
        ctx.print_line(f"- [{rec.kind}] {rec.role}: {rec.content}")


def _handle_prompt_file_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/prompt-file "):
        return None
    ctx.prompt_files.append(user[len("/prompt-file "):].strip())
    ctx.print_line(f"Added prompt file; count={len(ctx.prompt_files)}.")
    return True


def _handle_audit_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    command = parse_conversation_task_command(user)
    if command is None or command.valid:
        return None
    ctx.print_line(command.usage)
    return True


def _handle_unsupported_slash_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if parse_conversation_task_command(user) is not None:
        return None
    name = system_slash_command_name(user)
    if not name:
        return None
    if name == "show-prompt":
        return None
    ctx.print_line(f"不支持的系统命令：/{name}。输入 /help 查看当前界面支持的命令。")
    return True


def is_exit_command(user: str) -> bool:
    return user.lower() in {"/exit", "/logout", "/quit", "exit", "logout", "退出"}


def parse_expand_target(raw: str) -> str | None:
    if raw == "/expand":
        return "last"
    target = raw[len("/expand "):].strip()
    if not target:
        return "last"
    if target.isdigit():
        return target
    return None


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
