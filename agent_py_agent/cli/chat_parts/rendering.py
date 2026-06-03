
from __future__ import annotations

"""terminal rendering primitives for chat mode.

给人看的解释：
聊天界面的颜色、横线、启动 banner 和长回复折叠都放在这里，方便后续独立测试。
"""

import shutil
import sys
from collections.abc import Callable

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
    color_text,
    progress_bar,
    strip_ansi,
    style_text,
    supports_ansi,
)

# prompt_toolkit is optional
try:
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
except Exception:
    _pt_print = None
    _PT_ANSI = None

_TUI_OUTPUT_SINK: Callable[[str], None] | None = None
_TUI_STREAM_SINK: Callable[[str], None] | None = None
_TUI_STREAM_FINISH: Callable[[], None] | None = None


def set_tui_output_sink(sink: Callable[[str], None] | None) -> None:
    global _TUI_OUTPUT_SINK
    _TUI_OUTPUT_SINK = sink


def set_tui_stream_sink(
    sink: Callable[[str], None] | None,
    *,
    finish: Callable[[], None] | None = None,
) -> None:
    global _TUI_STREAM_FINISH, _TUI_STREAM_SINK
    _TUI_STREAM_SINK = sink
    _TUI_STREAM_FINISH = finish


def finish_tui_stream() -> None:
    if _TUI_STREAM_FINISH is not None:
        _TUI_STREAM_FINISH()


def _cprint(text: str) -> None:
    if _TUI_OUTPUT_SINK is not None:
        _TUI_OUTPUT_SINK(text + "\n")
        return
    if sys.stdout.isatty() and _pt_print is not None and _PT_ANSI is not None:
        rendered = _PT_ANSI(text) if supports_ansi() else strip_ansi(text)
        _pt_print(rendered)
    else:
        print(text if supports_ansi() else strip_ansi(text))


def _write_output_text(text: str) -> None:
    if _TUI_OUTPUT_SINK is not None:
        _TUI_OUTPUT_SINK(text)
        return
    sys.stdout.write(text if supports_ansi() else strip_ansi(text))
    sys.stdout.flush()


def _write_stream_text(text: str) -> None:
    if _TUI_STREAM_SINK is not None:
        _TUI_STREAM_SINK(text)
        return
    _write_output_text(text)


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


# Backward-compat wrapper: original signature was _tui_print_banner(agent, use_gateway)
def _tui_print_banner(agent, use_gateway: bool) -> None:
    text = startup_banner(agent.config.agent_name, use_gateway=use_gateway)
    for line in text.splitlines():
        _cprint(line)


def terminal_rule(char: str = "─", *, fallback: int = 119) -> str:

    width = max(20, shutil.get_terminal_size(fallback=(fallback, 24)).columns)
    return f"{GRAY}{char * width}{RESET}"
