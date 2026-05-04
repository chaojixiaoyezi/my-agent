from __future__ import annotations

"""LLM: terminal rendering primitives for chat mode.

给人看的解释：
聊天界面的颜色、横线、启动 banner 和长回复折叠都放在这里，方便后续独立测试。
"""

import shutil
import sys

# prompt_toolkit is optional
try:
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
except Exception:
    _pt_print = None
    _PT_ANSI = None


def _cprint(text: str) -> None:
    """Print ANSI-colored text via prompt_toolkit or fall back to print."""
    if _pt_print is not None and _PT_ANSI is not None:
        _pt_print(_PT_ANSI(text))
    else:
        print(text)

BLUE = "\033[38;2;59;130;246m"
GRAY = "\033[90m"
GREEN = "\033[38;2;34;197;94m"
YELLOW = "\033[38;2;234;179;8m"
CYAN = "\033[38;2;6;182;212m"
RESET = "\033[0m"
BOLD = "\033[1m"

COLLAPSE_PREVIEW_LINES = 12
COLLAPSE_PREVIEW_CHARS = 900
CONTEXT_WINDOW = 200_000


def progress_bar(ratio: float, width: int = 10) -> str:
    """Return a compact terminal progress bar."""

    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def collapse_response_text(text: str) -> tuple[str, bool]:
    """Return a terminal-friendly preview plus whether the text was collapsed."""

    lines = text.splitlines()
    if len(lines) <= COLLAPSE_PREVIEW_LINES and len(text) <= COLLAPSE_PREVIEW_CHARS:
        return text, False

    preview = "\n".join(lines[:COLLAPSE_PREVIEW_LINES]).strip()
    if len(preview) > COLLAPSE_PREVIEW_CHARS:
        preview = preview[:COLLAPSE_PREVIEW_CHARS].rstrip()
    if len(preview) < len(text):
        preview += "\n..."
    return preview, True


def startup_banner(agent_name: str, *, use_gateway: bool) -> str:
    """Build a compact startup banner inspired by terminal agents like Hermes."""

    mode = "gateway client" if use_gateway else "local runtime"
    return "\n".join(
        [
            f"{CYAN}    /\\_/\\{RESET}",
            f"{CYAN}   ( o.o ){RESET}   {BOLD}{agent_name}{RESET}",
            f"{CYAN}    > ^ <{RESET}    {GRAY}{mode}{RESET}",
            "",
        ]
    )


# Backward-compat wrapper: original signature was _tui_print_banner(agent, use_gateway)
def _tui_print_banner(agent, use_gateway: bool) -> None:
    text = startup_banner(agent.config.agent_name, use_gateway=use_gateway)
    for line in text.splitlines():
        print(line)


def terminal_rule(char: str = "─", *, fallback: int = 119) -> str:
    """Return a colored horizontal terminal rule."""

    width = max(20, shutil.get_terminal_size(fallback=(fallback, 24)).columns)
    return f"{GRAY}{char * width}{RESET}"
