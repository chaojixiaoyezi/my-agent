"""LLM: user input handling and history management for chat mode.

给人看的解释：
处理用户命令解析、斜杠命令路由、输入历史和命令执行。
TUI 和 fallback 两套循环都使用同一套命令处理逻辑。
"""

from __future__ import annotations

from .slash_command_types import SlashCommandContext

CHAT_HELP_TEXT = (
    "可用命令：\n"
    "/help                         显示帮助\n"
    "/status                       查看后台任务状态\n"
    "/expand [last|编号]           展开被自动折叠的助手回复\n"
    "/exit                         退出\n"
    "/memory [关键词]              搜索记忆\n"
    "/remember <内容>              手动写入记忆\n"
    "/btw                          显示运行时 prompt 注入\n"
    "/btw <内容>                   增加运行时 prompt 注入\n"
    "/btw-clear                    清空运行时 prompt 注入\n"
    "/prompt-file <路径>           增加动态 prompt 文件\n"
    "/subagents <数量> <目标>      生成 subagent 任务记录\n"
    "/show-prompt <问题>           显示最终 prompt 并回答\n"
)


def handle_common_slash_command(
    user: str,
    *,
    ctx: SlashCommandContext,
    include_fallback_help: bool = False,
) -> bool:
    """Handle slash commands shared by prompt_toolkit and fallback chat loops."""
    if user == "/help":
        suffix = "Ctrl+C                        退出\n其他输入                       正常对话\n" if include_fallback_help else ""
        ctx.print_line(CHAT_HELP_TEXT + suffix)
        return True
    if user.startswith("/remember "):
        rec = ctx.agent.remember(user[len("/remember "):], kind="note")
        ctx.print_line(f"已记忆: {rec.content}")
        return True
    if user.startswith("/memory"):
        query = user[len("/memory"):].strip()
        records = ctx.agent.recall(query, ctx.memory_limit) if query else ctx.agent.memory.all()[-ctx.memory_limit:]
        if not records:
            ctx.print_line("没有找到记忆。")
        else:
            for rec in records:
                ctx.print_line(f"- [{rec.kind}] {rec.role}: {rec.content}")
        return True
    if user == "/btw":
        if not ctx.runtime_inject:
            ctx.print_line("当前没有运行时 prompt 注入。")
        else:
            ctx.print_line("当前运行时 prompt 注入：")
            for index, item in enumerate(ctx.runtime_inject, 1):
                ctx.print_line(f"{index}. {item}")
        return True
    if user.startswith("/btw "):
        ctx.runtime_inject.append(user[len("/btw "):])
        ctx.print_line(f"已加入注入 prompt，当前 {len(ctx.runtime_inject)} 条。")
        return True
    if user == "/btw-clear":
        ctx.runtime_inject.clear()
        ctx.print_line("已清空运行时 prompt 注入。")
        return True
    if user.startswith("/prompt-file "):
        ctx.prompt_files.append(user[len("/prompt-file "):].strip())
        ctx.print_line(f"已加入 prompt 文件，当前 {len(ctx.prompt_files)} 个。")
        return True
    if user.startswith("/subagents "):
        parts = user.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            ctx.print_line("用法: /subagents <数量> <目标>")
            return True
        tasks = ctx.agent.spawn_subagents(parts[2], int(parts[1]))
        for task in tasks:
            ctx.print_line(f"- {task.id}: {task.goal}")
        return True
    return False


def is_exit_command(user: str) -> bool:
    """Check if user input is an exit command."""
    return user.lower() in {"/exit", "/logout", "/quit", "exit", "logout", "退出"}


def parse_expand_target(raw: str) -> str | None:
    """Parse /expand command target, returning 'last', digit string, or None."""
    if raw == "/expand":
        return "last"
    target = raw[len("/expand "):].strip()
    if not target:
        return "last"
    if target.isdigit():
        return target
    return None


def is_show_prompt_command(user: str) -> tuple[bool, str]:
    """Check if user input starts with /show-prompt.

    Returns (is_show_prompt, actual_prompt).
    """
    if user.startswith("/show-prompt "):
        return True, user[len("/show-prompt "):]
    return False, user


__all__ = [
    "CHAT_HELP_TEXT",
    "handle_common_slash_command",
    "is_exit_command",
    "parse_expand_target",
    "is_show_prompt_command",
]