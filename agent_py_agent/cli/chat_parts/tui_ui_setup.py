
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .rendering import _tui_print_banner, set_tui_output_sink, set_tui_stream_sink
from .tui import (
    TuiStatusRefs,
    _tui_get_activity_text,
    _tui_get_status_text,
    _tui_handle_expand_command,
)
from .tui_keybindings import TuiCreateKeybindingsParams, _tui_create_keybindings
from .tui_params import MakeTuiAppParams
from .tui_transcript_store import (
    APP_REDRAW_INTERVAL_SECONDS,
    TuiTranscriptStore,
)


@dataclass
class StatusBarConfig:
    refs: TuiStatusRefs
    model_name: str


APP_RENDER_POSTPONE_SECONDS = 1 / 60


def _make_activity_bar(
    config: StatusBarConfig,
):
    from prompt_toolkit.layout import FormattedTextControl, Window

    return Window(
        content=FormattedTextControl(
            lambda: [
                (
                    "class:activity-bar",
                    f" {_tui_get_activity_text(config.refs)} ",
                )
            ],
        ),
        height=1,
        style="class:activity-bar",
    )


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
    store = TuiTranscriptStore(output_area, follow_ref, app_ref)

    set_tui_output_sink(store.append_history)
    set_tui_stream_sink(store.append_stream, finish=store.finish_stream)


def _app_scrollback_enabled(args: Any) -> bool:
    if getattr(args, "plain", False):
        return False
    return bool(getattr(args, "app_scrollback", True) or getattr(args, "app", False))


def make_tui_app(params: MakeTuiAppParams):
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window

    history_file = params.agent.root / ".chat_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    status_config = _make_status_bar_config(params)
    activity_bar = _make_activity_bar(status_config)
    status_bar = _make_status_bar(status_config)
    input_area = _make_input_area(str(history_file))

    use_app_scrollback = _app_scrollback_enabled(params.args)
    output_area = _make_transcript_area() if use_app_scrollback else None
    transcript_follow_ref = [True] if use_app_scrollback else None
    input_row = VSplit([_make_input_prompt_window(), input_area])
    body = (
        [output_area, activity_bar, status_bar, Window(height=1), input_row]
        if output_area is not None else [activity_bar, status_bar, Window(height=1), input_row]
    )
    layout = Layout(HSplit(body), focused_element=input_area)

    kb = _make_tui_keybindings(params, input_area, output_area, transcript_follow_ref)
    style = _make_tui_style()

    app_ref: list[Any] = [None]
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=use_app_scrollback,
        erase_when_done=False,
        mouse_support=use_app_scrollback,
        min_redraw_interval=APP_REDRAW_INTERVAL_SECONDS if use_app_scrollback else None,
        max_render_postpone_time=APP_RENDER_POSTPONE_SECONDS if use_app_scrollback else 0.01,
    )
    _configure_transcript_sink(output_area, transcript_follow_ref, app_ref, app)

    return app


def _make_status_bar_config(params: MakeTuiAppParams) -> StatusBarConfig:
    return StatusBarConfig(
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


def _make_tui_keybindings(
    params: MakeTuiAppParams,
    input_area: Any,
    output_area: Any | None,
    transcript_follow_ref: list[bool] | None,
):
    return _tui_create_keybindings(
        TuiCreateKeybindingsParams(
            input_area,
            output_area,
            transcript_follow_ref,
            params.agent,
            params.args,
            params.runtime_inject,
            params.prompt_files,
            params.use_gateway,
            params.paths,
            params.state_lock,
            params.is_running_ref,
            params.pending_jobs_ref,
            params.running_prompt_ref,
            params.running_started_at_ref,
            params.shutting_down_ref,
            params.stop_event,
            params.assistant_outputs,
            params.jobs,
            params.pending_jobs_ref_for_enqueue,
        )
    )


def _make_tui_style():
    from prompt_toolkit.styles import Style

    return Style.from_dict(
        {"activity-bar": "bg:#121827 #d6e4ff bold", "status-bar": "bg:#1a1a2e #8ec5ff bold",
         "transcript": "#f8fafc", "input-area": "#f8fafc", "prompt": "#f8fafc bold"}
    )


def _configure_transcript_sink(
    output_area: Any | None,
    transcript_follow_ref: list[bool] | None,
    app_ref: list[Any],
    app: Any,
) -> None:
    if output_area is not None and transcript_follow_ref is not None:
        app_ref[0] = app
        _install_transcript_sink(output_area, transcript_follow_ref, app_ref)
        return
    set_tui_output_sink(None)
    set_tui_stream_sink(None)
