

from __future__ import annotations

from .fallback import ChatJob, run_fallback
from .gateway_client import (
    ChatRequestContent,
    check_gateway_alive,
    format_gateway_timing,
    poll_gateway_chunks,
    submit_chat_request,
)
from .history import (
    MAX_HISTORY_TURNS,
    append_conversation_turn,
    build_history_context,
)
from .input_loop import (
    CHAT_HELP_TEXT,
    handle_common_slash_command,
    is_exit_command,
    is_show_prompt_command,
    parse_expand_target,
)
from .renderer import (
    BLUE,
    BOLD,
    COLLAPSE_PREVIEW_CHARS,
    COLLAPSE_PREVIEW_LINES,
    CONTEXT_WINDOW,
    CYAN,
    GRAY,
    GREEN,
    RESET,
    YELLOW,
    collapse_response_text,
    progress_bar,
    startup_banner,
    terminal_rule,
)
from .session_state import (
    ConversationHistory,
    create_or_resume_session,
    touch_session_on_exit,
)
from .slash_command_types import SlashCommandContext
from .tui import run_tui

# Import thinking spinner for backward compatibility
try:
    from ...cli.thinking_spinner import ThinkingSpinner
except ImportError:
    ThinkingSpinner = None

# Import gateway helpers for backward compatibility
from ...agent.gateway import (
    render_gateway_status,
    wait_for_gateway_running,
)
from ...cli.models import ChatJob
from ..common import CHAT_PROMPT, FALLBACK_CHAT_PROMPT, make_agent, resume_context_override


def _collapse_response_text(text: str):
    return collapse_response_text(text)


def _progress_bar(ratio: float, width: int = 10) -> str:
    return progress_bar(ratio, width)


def _startup_banner(agent_name: str, *, use_gateway: bool) -> str:
    return startup_banner(agent_name, use_gateway=use_gateway)


def _terminal_rule(char: str = "─", *, fallback: int = 119) -> str:
    return terminal_rule(char, fallback=fallback)


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
    "run_fallback",
    "run_tui",
    "startup_banner",
    "terminal_rule",
    "wait_for_gateway_running",
]