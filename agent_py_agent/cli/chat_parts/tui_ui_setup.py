"""LLM: TUI UI setup — extracted from tui.py to keep run_tui under 100 lines.

给人看的解释：
tui.py 的 run_tui 超过 100 行限制。拆出 UI 初始化相关代码（status_bar、input_area、key_bindings、layout、style、app 创建）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from queue import Queue
from typing import Any

from .fallback_state import ChatJob
from .input_loop import is_show_prompt_command
from .rendering import _cprint, _tui_print_banner
from .tui import (
    _tui_get_status_text,
    _tui_handle_expand_command,
    _tui_request_exit,
)
from .tui_params import MakeTuiAppParams, TuiHandleCommandParams


@dataclass
class TuiCreateKeybindingsParams:
    """Parameter bundle for _tui_create_keybindings."""

    input_area: Any
    agent: Any
    args: Any
    runtime_inject: list[Any]
    prompt_files: list[Any]
    use_gateway: bool
    paths: Any
    state_lock: threading.Lock
    is_running_ref: list[Any]
    pending_jobs_ref: list[int]
    running_prompt_ref: list[str]
    running_started_at_ref: list[float]
    shutting_down_ref: list[bool]
    stop_event: threading.Event
    assistant_outputs: list[str]
    jobs: Queue[Any]
    pending_jobs_ref_for_enqueue: list[int]


def _tui_create_keybindings(params: TuiCreateKeybindingsParams):
    """Create prompt_toolkit KeyBindings with enter/c-c/c-d handlers."""
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    def _handle_enter(event):
        text = params.input_area.text.strip()
        if not text:
            return
        params.input_area.text = ""
        if _tui_handle_command(
            params=TuiHandleCommandParams(
                user=text,
                agent=params.agent,
                args=params.args,
                runtime_inject=params.runtime_inject,
                prompt_files=params.prompt_files,
                use_gateway=params.use_gateway,
                paths=params.paths,
                state_lock=params.state_lock,
                is_running_ref=params.is_running_ref,
                pending_jobs_ref=params.pending_jobs_ref,
                running_prompt_ref=params.running_prompt_ref,
                running_started_at_ref=params.running_started_at_ref,
                shutting_down_ref=params.shutting_down_ref,
                stop_event=params.stop_event,
                assistant_outputs=params.assistant_outputs,
            )
        ):
            if params.stop_event.is_set():
                event.app.exit()
            return
        show_prompt, text = is_show_prompt_command(text)

        job = ChatJob(
            user=text,
            show_prompt=show_prompt,
            inject=list(params.runtime_inject),
            prompt_files=list(params.prompt_files),
        )
        with params.state_lock:
            params.pending_jobs_ref_for_enqueue[0] += 1
        params.jobs.put(job)
        from .rendering import terminal_rule

        lines = [line.rstrip() for line in text.splitlines()] or [text]
        _cprint(f"\n{terminal_rule()}")
        for index, line in enumerate(lines):
            ball = "\033[38;2;59;130;246m●\033[0m" if index == 0 else " "
            _cprint(f"{ball}  \033[38;2;59;130;246m\033[1m{line}\033[0m")
        _cprint("")
        _cprint("")

    kb.add("enter")(_handle_enter)

    def _handle_ctrl_c(event):
        _tui_request_exit(
            params.shutting_down_ref,
            params.state_lock,
            params.is_running_ref,
            params.pending_jobs_ref,
            params.stop_event,
        )
        event.app.exit()

    kb.add("c-c")(_handle_ctrl_c)

    def _handle_ctrl_d(event):
        _tui_request_exit(
            params.shutting_down_ref,
            params.state_lock,
            params.is_running_ref,
            params.pending_jobs_ref,
            params.stop_event,
        )
        event.app.exit()

    kb.add("c-d")(_handle_ctrl_d)

    return kb


def _make_status_bar(
    state_lock,
    is_running_ref,
    pending_jobs_ref,
    running_started_at_ref,
    last_token_estimate_ref,
    model_name,
):
    """Create the status bar Window component."""
    from prompt_toolkit.layout import FormattedTextControl, Window

    return Window(
        content=FormattedTextControl(
            lambda: [
                (
                    "class:status-bar",
                    f" {_tui_get_status_text(state_lock, is_running_ref, pending_jobs_ref, running_started_at_ref, last_token_estimate_ref, model_name)} ",
                )
            ],
        ),
        height=1,
        style="class:status-bar",
    )


def _make_input_area(history_file_path: str) -> Any:
    """Create the TextArea input component with history."""
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import TextArea

    return TextArea(
        height=Dimension(min=1, max=8),
        prompt=[("class:prompt", "❯ ")],
        style="class:input-area",
        multiline=False,
        wrap_lines=False,
        history=FileHistory(history_file_path),
        auto_suggest=AutoSuggestFromHistory(),
    )


def make_tui_app(params: MakeTuiAppParams):
    """Build and return a prompt_toolkit Application with status bar and input area."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.styles import Style

    history_file = params.agent.root / ".chat_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    status_bar = _make_status_bar(
        params.state_lock,
        params.is_running_ref,
        params.pending_jobs_ref,
        params.running_started_at_ref,
        params.last_token_estimate_ref,
        params.agent.config.model_name,
    )
    input_area = _make_input_area(str(history_file))

    kb = _tui_create_keybindings(
        TuiCreateKeybindingsParams(
            input_area=input_area,
            agent=params.agent,
            args=params.args,
            runtime_inject=params.runtime_inject,
            prompt_files=params.prompt_files,
            use_gateway=params.use_gateway,
            paths=params.paths,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            running_prompt_ref=params.running_prompt_ref,
            running_started_at_ref=params.running_started_at_ref,
            shutting_down_ref=params.shutting_down_ref,
            stop_event=params.stop_event,
            assistant_outputs=params.assistant_outputs,
            jobs=params.jobs,
            pending_jobs_ref_for_enqueue=params.pending_jobs_ref_for_enqueue,
        )
    )

    layout = Layout(HSplit([status_bar, Window(height=1), input_area]))

    style = Style.from_dict(
        {
            "status-bar": "bg:#1a1a2e #8ec5ff bold",
            "input-area": "#f8fafc",
            "prompt": "#f8fafc bold",
        }
    )

    app = Application(
        layout=layout, key_bindings=kb, style=style, full_screen=False, mouse_support=False
    )

    return app
