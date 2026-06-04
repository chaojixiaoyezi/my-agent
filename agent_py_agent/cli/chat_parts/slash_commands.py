
from __future__ import annotations

from collections.abc import Callable

from ...agent.agent_core.subagent import SpawnSubagentsParams
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


def handle_common_slash_command(
    user: str,
    *,
    ctx: SlashCommandContext,
    include_plain_help: bool = False,
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
        result = handler(user, ctx, include_plain_help)
        if result is not None:
            return result
    return False


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
    rec = ctx.agent.remember(user[len("/remember "):], kind="note")
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


def _handle_btw_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
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


def _print_runtime_injections(ctx: SlashCommandContext) -> None:
    if not ctx.runtime_inject:
        ctx.print_line("No runtime prompt injections.")
        return
    ctx.print_line("Runtime prompt injections:")
    for index, item in enumerate(ctx.runtime_inject, 1):
        ctx.print_line(f"{index}. {item}")


def _handle_prompt_file_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/prompt-file "):
        return None
    ctx.prompt_files.append(user[len("/prompt-file "):].strip())
    ctx.print_line(f"Added prompt file; count={len(ctx.prompt_files)}.")
    return True


def _handle_subagents_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
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
