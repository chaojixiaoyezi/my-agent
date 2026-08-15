

from __future__ import annotations

import os
import re
import shutil
import sys
from functools import lru_cache

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
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def progress_bar(ratio: float, width: int = 10) -> str:
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def collapse_response_text(
    text: str,
    *,
    preview_lines: int = COLLAPSE_PREVIEW_LINES,
    preview_chars: int = COLLAPSE_PREVIEW_CHARS,
) -> tuple[str, bool]:
    lines = text.splitlines()
    preview_lines = max(0, int(preview_lines or 0))
    preview_chars = max(0, int(preview_chars or 0))
    if len(lines) <= preview_lines and len(text) <= preview_chars:
        return text, False
    preview = "\n".join(lines[:preview_lines]).strip() if preview_lines else ""
    if preview_chars and len(preview) > preview_chars:
        preview = preview[:preview_chars].rstrip()
    if len(preview) < len(text):
        preview += "\n..."
    return preview, True


def startup_banner(agent_name: str, *, use_gateway: bool) -> str:
    mode = "gateway client" if use_gateway else "local runtime"
    return "\n".join(
        [
            f"{CYAN}    /\\_/\\{RESET}",
            f"{CYAN}   ( o.o ){RESET}   {BOLD}{agent_name}{RESET}",
            f"{CYAN}    > ^ <{RESET}    {GRAY}{mode}{RESET}",
            "",
        ]
    )


def terminal_rule(char: str = "─", *, default_width: int = 119) -> str:
    width = max(20, shutil.get_terminal_size((default_width, 24)).columns)
    return f"{GRAY}{char * width}{RESET}"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def style_text(text: str, *codes: str) -> str:
    if not codes or not supports_ansi():
        return text
    return "".join(codes) + text + RESET


def color_text(text: str, color: str) -> str:
    return style_text(text, color)


def supports_ansi() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("MY_AGENT_NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return os.environ.get("TERM", "xterm").lower() != "dumb"
    return _windows_vt_enabled() or bool(
        os.environ.get(name)
        for name in ("WT_SESSION", "ANSICON", "ConEmuANSI", "TERM_PROGRAM")
    )


@lru_cache(maxsize=1)
def _windows_vt_enabled() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if handle in (0, -1) or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_virtual_terminal_processing = 0x0004
        if mode.value & enable_virtual_terminal_processing:
            return True
        return bool(
            kernel32.SetConsoleMode(
                handle,
                mode.value | enable_virtual_terminal_processing,
            )
        )
    except Exception:
        return False


__all__ = [
    "BLUE",
    "BOLD",
    "CYAN",
    "GRAY",
    "GREEN",
    "RESET",
    "YELLOW",
    "COLLAPSE_PREVIEW_CHARS",
    "COLLAPSE_PREVIEW_LINES",
    "CONTEXT_WINDOW",
    "collapse_response_text",
    "color_text",
    "progress_bar",
    "startup_banner",
    "strip_ansi",
    "style_text",
    "supports_ansi",
    "terminal_rule",
]
