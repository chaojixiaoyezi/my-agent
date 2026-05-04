"""LLM: TUI UI setup — extracted from tui.py to keep run_tui under 100 lines.

给人看的解释：
tui.py 的 run_tui 超过 100 行限制。拆出 UI 初始化相关代码（status_bar、input_area、key_bindings、layout、style、app 创建）。
"""

from __future__ import annotations

import threading
from typing import Any

from .input_loop import is_show_prompt_command
from .rendering import (
    _cprint,
    _tui_get_status_text,
    _tui_handle_expand_command,
    _tui_print_banner,
    _tui_request_exit,
)


def _tui_create_keybindings(
    input_area,
    agent,
    args,
    runtime_inject,
    prompt_files,
    use_gateway,
    paths,
    state_lock,
    is_running_ref,
    pending_jobs_ref,
    running_prompt_ref,
    running_started_at_ref,
    shutting_down_ref,
    stop_event,
    assistant_outputs,
    jobs,
    pending_jobs_ref_for_enqueue,
):
    """Create prompt_toolkit KeyBindings with enter/c-c/c-d handlers."""
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    def _handle_enter(event):
        text = input_area.text.strip()
        if not text:
            return
        input_area.text = ""
        if _tui_handle_command(
            text,
            agent,
            args,
            runtime_inject,
            prompt_files,
            use_gateway,
            paths,
            state_lock,
            is_running_ref,
            pending_jobs_ref,
            running_prompt_ref,
            running_started_at_ref,
            shutting_down_ref,
            stop_event,
            assistant_outputs,
        ):
            if stop_event.is_set():
                event.app.exit()
            return
        show_prompt, text = is_show_prompt_command(text)
        from .fallback import ChatJob

        job = ChatJob(
            user=text,
            show_prompt=show_prompt,
            inject=list(runtime_inject),
            prompt_files=list(prompt_files),
        )
        with state_lock:
            pending_jobs_ref_for_enqueue[0] += 1
        jobs.put(job)
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
            shutting_down_ref,
            state_lock,
            is_running_ref,
            pending_jobs_ref,
            stop_event,
        )
        event.app.exit()

    kb.add("c-c")(_handle_ctrl_c)

    def _handle_ctrl_d(event):
        _tui_request_exit(
            shutting_down_ref,
            state_lock,
            is_running_ref,
            pending_jobs_ref,
            stop_event,
        )
        event.app.exit()

    kb.add("c-d")(_handle_ctrl_d)

    return kb


def _tui_handle_command(
    user,
    agent,
    args,
    runtime_inject,
    prompt_files,
    use_gateway,
    paths,
    state_lock,
    is_running_ref,
    pending_jobs_ref,
    running_prompt_ref,
    running_started_at_ref,
    shutting_down_ref,
    stop_event,
    assistant_outputs,
) -> bool:
    """Handle TUI slash command. Returns True if should exit."""
    import time

    from .input_loop import handle_common_slash_command, is_exit_command

    if is_exit_command(user):
        _tui_request_exit(
            shutting_down_ref,
            state_lock,
            is_running_ref,
            pending_jobs_ref,
            stop_event,
        )
        return True
    if user == "/expand" or user.startswith("/expand "):
        return _tui_handle_expand_command(user, assistant_outputs)
    if user == "/status":
        with state_lock:
            active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
            prompt = running_prompt_ref[0]
            elapsed = time.perf_counter() - running_started_at_ref[0] if is_running_ref[0] else 0
        if not active:
            _cprint("当前没有后台任务。")
        elif is_running_ref[0]:
            _cprint(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {pending_jobs_ref[0]} 个任务。")
            _cprint(f"当前任务: {prompt}")
        else:
            _cprint(f"当前没有运行中的任务；队列中还有 {pending_jobs_ref[0]} 个任务。")
        if use_gateway:
            from ...agent.gateway import render_gateway_status

            for line in render_gateway_status(agent, paths):
                _cprint(line)
        return True
    return handle_common_slash_command(
        user,
        agent=agent,
        memory_limit=args.memory_limit,
        runtime_inject=runtime_inject,
        prompt_files=prompt_files,
        print_line=_cprint,
    )


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


def make_tui_app(
    agent,
    state_lock,
    is_running_ref,
    pending_jobs_ref,
    running_started_at_ref,
    last_token_estimate_ref,
    jobs,
    pending_jobs_ref_for_enqueue,
    runtime_inject,
    prompt_files,
    args,
    use_gateway,
    paths,
    assistant_outputs,
    shutting_down_ref,
    running_prompt_ref,
    stop_event,
):
    """Build and return a prompt_toolkit Application with status bar and input area."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.styles import Style

    history_file = agent.root / ".chat_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    status_bar = _make_status_bar(
        state_lock,
        is_running_ref,
        pending_jobs_ref,
        running_started_at_ref,
        last_token_estimate_ref,
        agent.config.model_name,
    )
    input_area = _make_input_area(str(history_file))

    kb = _tui_create_keybindings(
        input_area,
        agent,
        args,
        runtime_inject,
        prompt_files,
        use_gateway,
        paths,
        state_lock,
        is_running_ref,
        pending_jobs_ref,
        running_prompt_ref,
        running_started_at_ref,
        shutting_down_ref,
        stop_event,
        assistant_outputs,
        jobs,
        pending_jobs_ref_for_enqueue,
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
