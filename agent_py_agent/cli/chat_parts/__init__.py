

from __future__ import annotations

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
from .plain import ChatJob, run_plain
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

# Optional thinking spinner.
try:
    from ...cli.thinking_spinner import ThinkingSpinner
except ImportError:
    ThinkingSpinner = None

# Gateway helpers used by chat CLI.
from ...agent.gateway_parts import (
    render_gateway_status,
    wait_for_gateway_running,
)
from ...cli.models import ChatJob
from ..common import CHAT_PROMPT, PLAIN_CHAT_PROMPT, make_agent, resume_context_override

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
