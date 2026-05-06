
from __future__ import annotations

import threading
from dataclasses import dataclass
from queue import Queue
from typing import Any

from .fallback_state import ChatJob
from .input_loop import is_show_prompt_command
from .renderer import BLUE, BOLD, strip_ansi, style_text
from .rendering import _cprint, _tui_print_banner, set_tui_output_sink
from .tui import (
    TuiExitRefs,
    TuiStatusRefs,
    _tui_get_status_text,
    _tui_handle_command,
    _tui_handle_expand_command,
    _tui_request_exit,
)
from .tui_params import MakeTuiAppParams, TuiHandleCommandParams


@dataclass
class TuiCreateKeybindingsParams:

    input_area: Any
    transcript_area: Any | None
    transcript_follow_ref: list[bool] | None
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


@dataclass
class StatusBarConfig:
    refs: TuiStatusRefs
    model_name: str


MAX_TRANSCRIPT_CHARS = 200_000
TRANSCRIPT_SCROLL_LINES = 10


def _tui_enqueue_job(
    params: TuiCreateKeybindingsParams,
    text: str,
) -> None:
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
        ball = style_text("●", BLUE) if index == 0 else " "
        _cprint(f"{ball}  {style_text(line, BLUE, BOLD)}")
    _cprint("")
    _cprint("")


def _handle_enter_keybinding(event, params):
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
    _tui_enqueue_job(params, text)


def _set_transcript_follow(params: TuiCreateKeybindingsParams, value: bool) -> None:
    if params.transcript_follow_ref is not None:
        params.transcript_follow_ref[0] = value


def _move_transcript_cursor(area: Any, delta: int) -> None:
    if area is None:
        return
    if delta < 0:
        area.buffer.cursor_up(count=abs(delta))
    elif delta > 0:
        area.buffer.cursor_down(count=delta)


def _scroll_transcript(params: TuiCreateKeybindingsParams, delta: int) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, False)
    _move_transcript_cursor(params.transcript_area, delta)


def _scroll_transcript_home(params: TuiCreateKeybindingsParams) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, False)
    params.transcript_area.buffer.cursor_position = 0


def _scroll_transcript_end(params: TuiCreateKeybindingsParams) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, True)
    params.transcript_area.buffer.cursor_position = len(params.transcript_area.text)


def _handle_ctrl_c_keybinding(event, params):
    _tui_request_exit(
        TuiExitRefs(
            shutting_down_ref=params.shutting_down_ref,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            stop_event=params.stop_event,
        )
    )
    event.app.exit()


def _handle_ctrl_d_keybinding(event, params):
    _tui_request_exit(
        TuiExitRefs(
            shutting_down_ref=params.shutting_down_ref,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            stop_event=params.stop_event,
        )
    )
    event.app.exit()


def _tui_create_keybindings(params: TuiCreateKeybindingsParams):
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    kb = KeyBindings()
    kb.add("enter")(lambda e: _handle_enter_keybinding(e, params))
    kb.add("c-c")(lambda e: _handle_ctrl_c_keybinding(e, params))
    kb.add("c-d")(lambda e: _handle_ctrl_d_keybinding(e, params))
    if params.transcript_area is not None:
        kb.add("pageup")(lambda e: _scroll_transcript(params, -TRANSCRIPT_SCROLL_LINES))
        kb.add("pagedown")(lambda e: _scroll_transcript(params, TRANSCRIPT_SCROLL_LINES))
        kb.add("home")(lambda e: _scroll_transcript_home(params))
        kb.add("end")(lambda e: _scroll_transcript_end(params))
        kb.add(Keys.ScrollUp)(lambda e: _scroll_transcript(params, -3))
        kb.add(Keys.ScrollDown)(lambda e: _scroll_transcript(params, 3))
    return kb


def _make_status_bar(
    config: StatusBarConfig,
):
    from prompt_toolkit.layout import FormattedTextControl, Window

    return Window(
        content=FormattedTextControl(
            lambda: [
                (
                    "class:status-bar",
                    f" {_tui_get_status_text(config.refs, config.model_name)} ",
                )
            ],
        ),
        height=1,
        style="class:status-bar",
    )


def _make_input_area(history_file_path: str) -> Any:
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import TextArea

    return TextArea(
        height=Dimension(min=1, max=8),
        style="class:input-area",
        multiline=False,
        wrap_lines=False,
        history=FileHistory(history_file_path),
        auto_suggest=AutoSuggestFromHistory(),
    )


def _make_input_prompt_window() -> Any:
    from prompt_toolkit.layout import FormattedTextControl, Window

    return Window(
        content=FormattedTextControl([("class:prompt", "❯ ")]),
        width=2,
        dont_extend_width=True,
        style="class:prompt",
    )


def _make_transcript_area() -> Any:
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import TextArea

    return TextArea(
        text="",
        multiline=True,
        read_only=True,
        focusable=True,
        focus_on_click=False,
        wrap_lines=True,
        scrollbar=True,
        height=Dimension(weight=1),
        style="class:transcript",
    )


def _install_transcript_sink(output_area: Any, follow_ref: list[bool], app_ref: list[Any]) -> None:
    transcript = [""]
    lock = threading.Lock()

    def append_text(text: str) -> None:
        cleaned = strip_ansi(text)
        with lock:
            transcript[0] += cleaned
            if len(transcript[0]) > MAX_TRANSCRIPT_CHARS:
                transcript[0] = transcript[0][-MAX_TRANSCRIPT_CHARS:]
            output_area.text = transcript[0]
            if follow_ref[0]:
                output_area.buffer.cursor_position = len(output_area.text)
        if app_ref[0] is not None:
            app_ref[0].invalidate()

    set_tui_output_sink(append_text)


def _app_scrollback_enabled(args: Any) -> bool:
    return bool(getattr(args, "app_scrollback", False) or getattr(args, "app", False))


def make_tui_app(params: MakeTuiAppParams):
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
    from prompt_toolkit.styles import Style

    history_file = params.agent.root / ".chat_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    status_bar = _make_status_bar(
        StatusBarConfig(
            refs=TuiStatusRefs(
                params.state_lock,
                params.is_running_ref,
                params.pending_jobs_ref,
                params.running_started_at_ref,
                params.last_token_estimate_ref,
                params.thinking_line_ref,
            ),
            model_name=params.agent.config.model_name,
        )
    )
    input_area = _make_input_area(str(history_file))

    use_app_scrollback = _app_scrollback_enabled(params.args)
    output_area = _make_transcript_area() if use_app_scrollback else None
    transcript_follow_ref = [True] if use_app_scrollback else None
    input_row = VSplit([_make_input_prompt_window(), input_area])
    body = (
        [output_area, status_bar, Window(height=1), input_row]
        if output_area is not None else [status_bar, Window(height=1), input_row]
    )
    layout = Layout(HSplit(body), focused_element=input_area)

    kb = _tui_create_keybindings(
        TuiCreateKeybindingsParams(
            input_area, output_area, transcript_follow_ref, params.agent, params.args, params.runtime_inject, params.prompt_files, params.use_gateway, params.paths, params.state_lock, params.is_running_ref, params.pending_jobs_ref, params.running_prompt_ref, params.running_started_at_ref, params.shutting_down_ref, params.stop_event, params.assistant_outputs, params.jobs, params.pending_jobs_ref_for_enqueue
        )
    )

    style = Style.from_dict(
        {
            "status-bar": "bg:#1a1a2e #8ec5ff bold",
            "transcript": "#f8fafc",
            "input-area": "#f8fafc",
            "prompt": "#f8fafc bold",
        }
    )

    app_ref: list[Any] = [None]
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=use_app_scrollback,
        erase_when_done=False,
        mouse_support=use_app_scrollback,
    )
    if output_area is not None and transcript_follow_ref is not None:
        app_ref[0] = app
        _install_transcript_sink(output_area, transcript_follow_ref, app_ref)
    else:
        set_tui_output_sink(None)

    return app
