from __future__ import annotations

import importlib
from typing import Any

# LLM: This facade keeps legacy chat_parts imports compatible while lazily loading leaf modules.
# Importing history, renderer, or TUI setup must not initialize Gateway workers or the full agent.
# 模块用途: 汇总聊天界面的公共函数和常量，并按需加载；单个界面组件可以独立快速导入。


_EXPORTS: dict[str, tuple[str, str]] = {
    "ChatRequestContent": ("gateway_client", "ChatRequestContent"),
    "check_gateway_alive": ("gateway_client", "check_gateway_alive"),
    "format_gateway_timing": ("gateway_client", "format_gateway_timing"),
    "poll_gateway_chunks": ("gateway_client", "poll_gateway_chunks"),
    "submit_chat_request": ("gateway_client", "submit_chat_request"),
    "MAX_HISTORY_TURNS": ("history", "MAX_HISTORY_TURNS"),
    "append_conversation_turn": ("history", "append_conversation_turn"),
    "build_history_context": ("history", "build_history_context"),
    "CHAT_HELP_TEXT": ("input_loop", "CHAT_HELP_TEXT"),
    "handle_common_slash_command": ("input_loop", "handle_common_slash_command"),
    "is_exit_command": ("input_loop", "is_exit_command"),
    "is_show_prompt_command": ("input_loop", "is_show_prompt_command"),
    "parse_expand_target": ("input_loop", "parse_expand_target"),
    "run_plain": ("plain", "run_plain"),
    "BLUE": ("renderer", "BLUE"),
    "BOLD": ("renderer", "BOLD"),
    "COLLAPSE_PREVIEW_CHARS": ("renderer", "COLLAPSE_PREVIEW_CHARS"),
    "COLLAPSE_PREVIEW_LINES": ("renderer", "COLLAPSE_PREVIEW_LINES"),
    "CONTEXT_WINDOW": ("renderer", "CONTEXT_WINDOW"),
    "CYAN": ("renderer", "CYAN"),
    "GRAY": ("renderer", "GRAY"),
    "GREEN": ("renderer", "GREEN"),
    "RESET": ("renderer", "RESET"),
    "YELLOW": ("renderer", "YELLOW"),
    "collapse_response_text": ("renderer", "collapse_response_text"),
    "progress_bar": ("renderer", "progress_bar"),
    "startup_banner": ("renderer", "startup_banner"),
    "terminal_rule": ("renderer", "terminal_rule"),
    "ConversationHistory": ("session_state", "ConversationHistory"),
    "create_or_resume_session": ("session_state", "create_or_resume_session"),
    "touch_session_on_exit": ("session_state", "touch_session_on_exit"),
    "SlashCommandContext": ("slash_command_types", "SlashCommandContext"),
    "run_tui": ("tui", "run_tui"),
    "ThinkingSpinner": ("..thinking_spinner", "ThinkingSpinner"),
    "render_gateway_status": ("...agent.gateway_parts.status_rendering", "render_gateway_status"),
    "wait_for_gateway_running": (
        "...agent.gateway_parts.status_rendering",
        "wait_for_gateway_running",
    ),
    "ChatJob": ("..models", "ChatJob"),
    "CHAT_PROMPT": ("..common", "CHAT_PROMPT"),
    "PLAIN_CHAT_PROMPT": ("..common", "PLAIN_CHAT_PROMPT"),
    "make_agent": ("..common", "make_agent"),
    "resume_context_override": ("..common", "resume_context_override"),
}

__all__ = [
    "BLUE",
    "BOLD",
    "CYAN",
    "GRAY",
    "GREEN",
    "RESET",
    "YELLOW",
    "ChatJob",
    "CHAT_HELP_TEXT",
    "COLLAPSE_PREVIEW_CHARS",
    "COLLAPSE_PREVIEW_LINES",
    "CONTEXT_WINDOW",
    "MAX_HISTORY_TURNS",
    "ThinkingSpinner",
    "append_conversation_turn",
    "build_history_context",
    "check_gateway_alive",
    "collapse_response_text",
    "create_or_resume_session",
    "format_gateway_timing",
    "handle_common_slash_command",
    "is_exit_command",
    "is_show_prompt_command",
    "parse_expand_target",
    "progress_bar",
    "render_gateway_status",
    "resume_context_override",
    "run_plain",
    "run_tui",
    "startup_banner",
    "terminal_rule",
    "wait_for_gateway_running",
]


# LLM: Resolve declared exports or actual sibling modules lazily and cache the result. Relative
# targets may leave this package to preserve the former facade without eager imports.
# 函数用途: 第一次访问聊天公共符号或子模块时才导入，后续访问直接复用缓存。
def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is not None:
        module_name, attribute_name = target
        if not module_name.startswith("."):
            module_name = f".{module_name}"
        module = importlib.import_module(module_name, __name__)
        value = getattr(module, attribute_name)
        globals()[name] = value
        return value
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name == f"{__name__}.{name}":
            raise AttributeError(name) from None
        raise
    globals()[name] = module
    return module


# LLM: Include lazy exports in introspection without importing their implementation modules.
# 函数用途: 让补全、调试器和 dir() 显示聊天包的完整公开入口。
def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
