from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rendering import set_tui_output_sink, set_tui_stream_sink
from .tui import TuiStatusRefs, _tui_status_fragments
from .tui_keybindings import TuiCreateKeybindingsParams, _tui_create_keybindings
from .tui_params import MakeTuiAppParams
from .tui_transcript_store import (
    APP_REDRAW_INTERVAL_SECONDS,
    TuiTranscriptStore,
    set_active_transcript_store,
)


@dataclass
class StatusBarConfig:
    refs: TuiStatusRefs
    model_name: str
    workspace_name: str


@dataclass
class TranscriptSinkRequest:
    output_area: Any | None
    transcript_follow_ref: list[bool] | None
    app_ref: list[Any]
    app: Any
    params: MakeTuiAppParams


APP_RENDER_POSTPONE_SECONDS = 1 / 60

# 会话运行时 风格按键提示(styles.md: 次级信息 dim; 与 tui_keybindings 实际绑定一致)。
_HINT_TEXT = (
    " Enter 发送 · Ctrl+C 打断/退出 · Ctrl+L 清屏 · Ctrl+O 复制上一条回复 · "
    "Alt+R 详细档位 · /status /stop /goal /btw /verbose /help"
)


def _make_status_bar(config: StatusBarConfig):
    from prompt_toolkit.layout import FormattedTextControl, Window

    return Window(
        content=FormattedTextControl(
            lambda: _tui_status_fragments(
                config.refs,
                config.model_name,
                workspace=config.workspace_name,
            )
        ),
        height=1,
        style="class:status-bar",
    )


def _make_hint_bar():
    from prompt_toolkit.layout import FormattedTextControl, Window

    return Window(
        content=FormattedTextControl([("class:hint", _HINT_TEXT)]),
        height=1,
        style="class:hint",
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
        content=FormattedTextControl([("class:prompt", "> ")]),
        width=2,
        dont_extend_width=True,
        style="class:prompt",
    )


def _make_transcript_area() -> Any:
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import TextArea

    from .tui_lexer import TranscriptLexer

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
        lexer=TranscriptLexer(),
    )


def _install_transcript_sink(
    output_area: Any,
    follow_ref: list[bool],
    app_ref: list[Any],
    *,
    max_chars: int = 500_000,
) -> None:
    store = TuiTranscriptStore(output_area, follow_ref, app_ref, max_chars=max_chars)
    set_active_transcript_store(store)

    set_tui_output_sink(store.append_history)
    set_tui_stream_sink(store.append_stream, finish=store.finish_stream)


def _app_scrollback_enabled(args: Any) -> bool:
    if getattr(args, "plain", False):
        return False
    return bool(getattr(args, "app_scrollback", True))


def make_tui_app(params: MakeTuiAppParams):
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window

    history_file = params.agent.root / ".chat_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    status_config = _make_status_bar_config(params)
    status_bar = _make_status_bar(status_config)
    hint_bar = _make_hint_bar()
    input_area = _make_input_area(str(history_file))

    use_app_scrollback = _app_scrollback_enabled(params.args)
    output_area = _make_transcript_area() if use_app_scrollback else None
    transcript_follow_ref = [True] if use_app_scrollback else None
    input_row = VSplit([_make_input_prompt_window(), input_area])
    # 会话运行时 布局: 顶部状态行(品牌·模型·目录·活动), 中间对话流, 底部提示行+输入。
    body = (
        [status_bar, output_area, hint_bar, input_row]
        if output_area is not None else [status_bar, hint_bar, input_row]
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
    _configure_transcript_sink(TranscriptSinkRequest(output_area, transcript_follow_ref, app_ref, app, params))

    return app


def _make_status_bar_config(params: MakeTuiAppParams) -> StatusBarConfig:
    config = params.agent.config
    root = getattr(params.agent, "root", None)
    try:
        workspace_name = Path(root).name if root else ""
    except (TypeError, ValueError):
        workspace_name = ""
    return StatusBarConfig(
        refs=TuiStatusRefs(
            params.state_lock,
            params.is_running_ref,
            params.pending_jobs_ref,
            params.running_started_at_ref,
            params.last_token_estimate_ref,
            params.thinking_line_ref,
        ),
        model_name=config.model_name,
        workspace_name=str(workspace_name or ""),
    )


def _make_tui_keybindings(
    app_config: MakeTuiAppParams,
    input_area: Any,
    output_area: Any | None,
    transcript_follow_ref: list[bool] | None,
):
    return _tui_create_keybindings(
        TuiCreateKeybindingsParams(
            input_area,
            output_area,
            transcript_follow_ref,
            app_config.agent,
            app_config.args,
            app_config.runtime_inject,
            app_config.prompt_files,
            app_config.use_gateway,
            app_config.paths,
            app_config.state_lock,
            app_config.is_running_ref,
            app_config.pending_jobs_ref,
            app_config.running_prompt_ref,
            app_config.running_request_id_ref,
            app_config.running_started_at_ref,
            app_config.shutting_down_ref,
            app_config.stop_event,
            app_config.assistant_outputs,
            app_config.jobs,
            app_config.pending_jobs_ref_for_enqueue,
            app_config.current_session_id,
            int(getattr(app_config.agent.config, "chat_transcript_scroll_lines", 10) or 10),
        )
    )


def _make_tui_style():
    from prompt_toolkit.styles import Style

    # 对齐 会话运行时s.md: 正文默认前景色, 头部 bold, 次级 dim; cyan=用户/
    # 状态指示, green=成功, red=错误, magenta=品牌 marker。不用重底色块。
    return Style.from_dict(
        {
            "status-brand": "ansimagenta bold",
            "status-meta": "ansigray",
            "activity": "ansicyan",
            "hint": "ansigray",
            "transcript": "",
            "input-area": "",
            "prompt": "ansicyan bold",
            "user-prompt": "ansicyan bold",
            "assistant-marker": "ansimagenta bold",
            "footer": "ansigray",
            "error": "ansired",
        }
    )


def _configure_transcript_sink(request: TranscriptSinkRequest) -> None:
    if request.output_area is not None and request.transcript_follow_ref is not None:
        request.app_ref[0] = request.app
        max_chars = int(getattr(request.params.agent.config, "chat_transcript_max_chars", 500_000) or 500_000)
        _install_transcript_sink(request.output_area, request.transcript_follow_ref, request.app_ref, max_chars=max_chars)
        return
    set_active_transcript_store(None)
    set_tui_output_sink(None)
    set_tui_stream_sink(None)
