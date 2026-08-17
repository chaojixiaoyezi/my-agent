
"""Prompt-toolkit key bindings for the chat TUI."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from queue import Queue
from typing import Any

from ...agent.conversation.control_commands import parse_conversation_task_command
from ...agent.conversation.models import new_id
from .input_loop import is_show_prompt_command
from .plain_state import ChatJob
from .rendering import _cprint
from .tui import (
    TuiExitRefs,
    _tui_handle_command,
    _tui_request_exit,
)
from .tui_params import TuiHandleCommandParams

TRANSCRIPT_SCROLL_LINES = 10


@dataclass
class TuiCreateKeybindingsParams:
    """bundle for creating TUI keybindings without growing setup signatures."""

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
    running_request_id_ref: list[str]
    running_started_at_ref: list[float]
    shutting_down_ref: list[bool]
    stop_event: threading.Event
    assistant_outputs: list[str]
    jobs: Queue[Any]
    pending_jobs_ref_for_enqueue: list[int]
    current_session_id: str
    transcript_scroll_lines: int = TRANSCRIPT_SCROLL_LINES


def _tui_create_keybindings(params: TuiCreateKeybindingsParams):
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    kb = KeyBindings()
    kb.add("enter")(lambda e: _handle_enter_keybinding(e, params))
    kb.add("c-c")(lambda e: _handle_ctrl_c_keybinding(e, params))
    kb.add("c-d")(lambda e: _handle_ctrl_d_keybinding(e, params))
    # 会话运行时 风格快捷键: Ctrl+L 清屏, Ctrl+O 复制上一条回复, Alt+R 切换详细档位。
    kb.add("c-l")(lambda e: _handle_ctrl_l_keybinding(e, params))
    kb.add("c-o")(lambda e: _handle_ctrl_o_keybinding(e, params))
    kb.add("escape", "r")(lambda e: _handle_alt_r_keybinding(e, params))
    if params.transcript_area is not None:
        kb.add("pageup")(lambda e: _scroll_transcript(params, -params.transcript_scroll_lines))
        kb.add("pagedown")(lambda e: _scroll_transcript(params, params.transcript_scroll_lines))
        kb.add("home")(lambda e: _scroll_transcript_home(params))
        kb.add("end")(lambda e: _scroll_transcript_end(params))
        kb.add(Keys.ScrollUp)(lambda e: _scroll_transcript(params, -3))
        kb.add(Keys.ScrollDown)(lambda e: _scroll_transcript(params, 3))
    return kb


def _handle_enter_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    text = params.input_area.text.strip()
    if not text:
        return
    params.input_area.text = ""
    if _tui_handle_command(params=_handle_command_params(params, text)):
        if params.stop_event.is_set():
            event.app.exit()
        return
    _tui_enqueue_job(params, text)


def _handle_command_params(params: TuiCreateKeybindingsParams, text: str) -> TuiHandleCommandParams:
    return TuiHandleCommandParams(
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
        running_request_id_ref=params.running_request_id_ref,
        running_started_at_ref=params.running_started_at_ref,
        shutting_down_ref=params.shutting_down_ref,
        stop_event=params.stop_event,
        assistant_outputs=params.assistant_outputs,
        current_session_id=params.current_session_id,
    )


def _tui_enqueue_job(params: TuiCreateKeybindingsParams, text: str) -> None:
    display_text = text
    show_prompt, text = is_show_prompt_command(text)
    task_command = parse_conversation_task_command(text)
    system_task: dict[str, object] = {}
    if task_command is not None and task_command.valid:
        text = task_command.prompt
        system_task = task_command.to_request_payload()
    job = ChatJob(
        user=text,
        show_prompt=show_prompt,
        inject=list(params.runtime_inject),
        prompt_files=list(params.prompt_files),
        request_id=new_id("chat"),
        system_task=system_task,
    )
    with params.state_lock:
        params.pending_jobs_ref_for_enqueue[0] += 1
    params.jobs.put(job)
    _print_enqueued_prompt(display_text)


def _print_enqueued_prompt(text: str) -> None:
    # 会话运行时 用户行: 每行 "> " 前缀, 由 TranscriptLexer 渲染成青色。
    lines = [line.rstrip() for line in text.splitlines()] or [text]
    _cprint("")
    for line in lines:
        _cprint(f"> {line}")
    _cprint("")


def _handle_ctrl_c_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    # 会话运行时 语义: Ctrl+C 打断当前回合; 没有运行内容时再按才是退出。
    with params.state_lock:
        active = bool(params.is_running_ref[0]) or int(params.pending_jobs_ref[0] or 0) > 0
    if active:
        _tui_handle_command(params=_handle_command_params(params, "/stop"))
        return
    _request_exit(params)
    event.app.exit()


def _handle_ctrl_d_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    _request_exit(params)
    event.app.exit()


def _handle_ctrl_l_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    """会话运行时 Ctrl+L: 清屏(只清对话流显示, 历史与后台状态保留)。"""
    from .tui_transcript_store import clear_active_transcript

    if not clear_active_transcript() and params.transcript_area is not None:
        params.transcript_area.text = ""
        if params.transcript_area.buffer is not None:
            params.transcript_area.buffer.cursor_position = 0
    event.app.invalidate()


def _handle_ctrl_o_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    """会话运行时 Ctrl+O: 复制最近一条助手回复到剪贴板。"""
    if not params.assistant_outputs:
        return
    try:
        event.app.clipboard.set_text(params.assistant_outputs[-1])
    except Exception:  # noqa: BLE001 剪贴板不可用时静默(与 会话运行时 一致)
        pass
    event.app.invalidate()


def _handle_alt_r_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    """会话运行时 Alt+R(toggle raw output)映射为 /verbose 档位切换。"""
    _tui_handle_command(params=_handle_command_params(params, "/verbose"))


def _request_exit(params: TuiCreateKeybindingsParams) -> None:
    _tui_request_exit(
        TuiExitRefs(
            shutting_down_ref=params.shutting_down_ref,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            stop_event=params.stop_event,
        )
    )


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
