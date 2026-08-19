
# LLM: 本模块是 chat 系统斜杠命令的目录与分派入口；帮助/补全共享 CHAT_SLASH_COMMANDS，执行权仍归 typed parser 和显式 handler。
# 模块用途: 列出当前聊天命令、渲染帮助文字，并即时处理控制、记忆、提示文件和保证档命令。

from __future__ import annotations

from collections.abc import Callable

from ...agent.conversation.control_commands import (
    parse_conversation_control,
    parse_conversation_task_command,
    system_slash_command_name,
)
from .slash_command_types import CHAT_SLASH_COMMANDS, SlashCommandContext


# LLM: help renderer 只投影结构化目录项，保持既有 30 列用法栏；不能把帮助文本反向作为命令解析器。
# 函数用途: 生成 plain/TUI 共用的命令帮助正文。
def _render_chat_help_text() -> str:
    lines = ["Available commands:"]
    lines.extend(f"{spec.usage:<30} {spec.summary}" for spec in CHAT_SLASH_COMMANDS)
    return "\n".join(lines) + "\n"


CHAT_HELP_TEXT = _render_chat_help_text()


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
    if getattr(ctx.agent, "gateway_client_only", False) is True:
        result = ctx.agent.request_memory(
            operation="remember",
            session_id=ctx.conversation_id,
            content=user[len("/remember "):],
            limit=1,
        )
        records = result.get("records") if isinstance(result, dict) else []
        if result.get("ok") and isinstance(records, list) and records:
            ctx.print_line(f"Remembered: {str(records[0].get('content') or '')}")
        else:
            ctx.print_line(
                "记忆保存失败："
                + str(result.get("error_code") or "GATEWAY_UNAVAILABLE")
            )
        return True
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
    if getattr(ctx.agent, "gateway_client_only", False) is True:
        result = ctx.agent.request_memory(
            operation="search" if query else "recent",
            session_id=ctx.conversation_id,
            query=query,
            limit=ctx.memory_limit,
        )
        if not result.get("ok"):
            ctx.print_line(
                "记忆读取失败："
                + str(result.get("error_code") or "GATEWAY_UNAVAILABLE")
            )
            return True
        records = result.get("records")
        _print_memory_records(ctx, records if isinstance(records, list) else [])
        return True
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
        if isinstance(rec, dict):
            kind = str(rec.get("kind") or "")
            role = str(rec.get("role") or "")
            content = str(rec.get("content") or "")
        else:
            kind = str(getattr(rec, "kind", "") or "")
            role = str(getattr(rec, "role", "") or "")
            content = str(getattr(rec, "content", "") or "")
        ctx.print_line(f"- [{kind}] {role}: {content}")


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
    "CHAT_SLASH_COMMANDS",
    "handle_common_slash_command",
    "is_exit_command",
    "is_show_prompt_command",
    "parse_expand_target",
]
