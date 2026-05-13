# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

"""Prompt-toolkit key bindings for the chat TUI."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from queue import Queue
from typing import Any

from .fallback_state import ChatJob
from .input_loop import is_show_prompt_command
from .renderer import BLUE, BOLD, style_text
from .rendering import _cprint
from .tui import (
    TuiExitRefs,
    _tui_handle_command,
    _tui_request_exit,
)
from .tui_params import TuiHandleCommandParams

TRANSCRIPT_SCROLL_LINES = 10


# LLM: TuiCreateKeybindingsParams 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
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
    running_started_at_ref: list[float]
    shutting_down_ref: list[bool]
    stop_event: threading.Event
    assistant_outputs: list[str]
    jobs: Queue[Any]
    pending_jobs_ref_for_enqueue: list[int]
    transcript_scroll_lines: int = TRANSCRIPT_SCROLL_LINES


# LLM: _tui_create_keybindings 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护 TUI 聊天界面的输入、状态栏、退出或渲染行为。
def _tui_create_keybindings(params: TuiCreateKeybindingsParams):
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    kb = KeyBindings()
    kb.add("enter")(lambda e: _handle_enter_keybinding(e, params))
    kb.add("c-c")(lambda e: _handle_ctrl_c_keybinding(e, params))
    kb.add("c-d")(lambda e: _handle_ctrl_d_keybinding(e, params))
    if params.transcript_area is not None:
        kb.add("pageup")(lambda e: _scroll_transcript(params, -params.transcript_scroll_lines))
        kb.add("pagedown")(lambda e: _scroll_transcript(params, params.transcript_scroll_lines))
        kb.add("home")(lambda e: _scroll_transcript_home(params))
        kb.add("end")(lambda e: _scroll_transcript_end(params))
        kb.add(Keys.ScrollUp)(lambda e: _scroll_transcript(params, -3))
        kb.add(Keys.ScrollDown)(lambda e: _scroll_transcript(params, 3))
    return kb


# LLM: _handle_enter_keybinding 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
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


# LLM: _handle_command_params 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
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
        running_started_at_ref=params.running_started_at_ref,
        shutting_down_ref=params.shutting_down_ref,
        stop_event=params.stop_event,
        assistant_outputs=params.assistant_outputs,
    )


# LLM: _tui_enqueue_job 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护 TUI 聊天界面的输入、状态栏、退出或渲染行为。
def _tui_enqueue_job(params: TuiCreateKeybindingsParams, text: str) -> None:
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
    _print_enqueued_prompt(text)


# LLM: _print_enqueued_prompt 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_enqueued_prompt(text: str) -> None:
    from .rendering import terminal_rule

    lines = [line.rstrip() for line in text.splitlines()] or [text]
    _cprint(f"\n{terminal_rule()}")
    for index, line in enumerate(lines):
        ball = style_text("●", BLUE) if index == 0 else " "
        _cprint(f"{ball}  {style_text(line, BLUE, BOLD)}")
    _cprint("")
    _cprint("")


# LLM: _handle_ctrl_c_keybinding 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_ctrl_c_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    _request_exit(params)
    event.app.exit()


# LLM: _handle_ctrl_d_keybinding 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_ctrl_d_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    _request_exit(params)
    event.app.exit()


# LLM: _request_exit 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _set_transcript_follow 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _set_transcript_follow(params: TuiCreateKeybindingsParams, value: bool) -> None:
    if params.transcript_follow_ref is not None:
        params.transcript_follow_ref[0] = value


# LLM: _move_transcript_cursor 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _move_transcript_cursor(area: Any, delta: int) -> None:
    if area is None:
        return
    if delta < 0:
        area.buffer.cursor_up(count=abs(delta))
    elif delta > 0:
        area.buffer.cursor_down(count=delta)


# LLM: _scroll_transcript 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _scroll_transcript(params: TuiCreateKeybindingsParams, delta: int) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, False)
    _move_transcript_cursor(params.transcript_area, delta)


# LLM: _scroll_transcript_home 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _scroll_transcript_home(params: TuiCreateKeybindingsParams) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, False)
    params.transcript_area.buffer.cursor_position = 0


# LLM: _scroll_transcript_end 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _scroll_transcript_end(params: TuiCreateKeybindingsParams) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, True)
    params.transcript_area.buffer.cursor_position = len(params.transcript_area.text)
