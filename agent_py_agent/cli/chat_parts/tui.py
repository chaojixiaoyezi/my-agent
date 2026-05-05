"""LLM: TUI input loop and prompt_toolkit integration for chat mode.

给人看的解释：
prompt_toolkit 的 Application 封装、状态栏刷新、worker 线程管理，
都在这里实现，让 chat.py 的主循环保持简洁。
"""

from __future__ import annotations

import threading

from .rendering import (
    _cprint,
    _tui_print_banner,
    collapse_response_text,
    progress_bar,
    terminal_rule,
)
from .tui_params import (
    MakeTuiAppParams,
    StartWorkerParams,
    TuiHandleCommandParams,
    TuiRunParams,
    WorkerConfigParams,
)

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:  # pragma: no cover
    Application = None

MAX_HISTORY_TURNS = 8
CONTEXT_WINDOW = 200_000
COLLAPSE_PREVIEW_CHARS = 900


def _tui_get_status_text(
    state_lock: threading.Lock,
    is_running_ref: list,
    pending_jobs_ref: list,
    running_started_at_ref: list,
    last_token_estimate_ref: list,
    model: str,
) -> str:
    with state_lock:
        active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
        tokens = last_token_estimate_ref[0]
        started_at = running_started_at_ref[0]
    pct = tokens / CONTEXT_WINDOW if CONTEXT_WINDOW else 0
    bar = progress_bar(pct)
    pieces = [
        f"⚕ {model}",
        f"ctx {tokens / 1000:.1f}K/{CONTEXT_WINDOW / 1000:.0f}K",
        f"[{bar}] {pct:.0%}",
    ]
    return " | ".join(pieces)


def _tui_render_user_entry(text: str) -> None:
    lines = [line.rstrip() for line in text.splitlines()] or [text]
    _cprint(f"\n{terminal_rule()}")
    for index, line in enumerate(lines):
        ball = "\033[38;2;59;130;246m●\033[0m" if index == 0 else " "
        _cprint(f"{ball}  \033[38;2;59;130;246m\033[1m{line}\033[0m")
    _cprint("")
    _cprint("")


def _tui_render_assistant_response(
    text: str, assistant_outputs: list[str], agent_name: str
) -> None:
    preview, collapsed = collapse_response_text(text)
    assistant_outputs.append(text)
    message_id = len(assistant_outputs)
    _cprint(
        f"\033[38;2;34;197;94m{agent_name}#{message_id}>\033[0m {preview if collapsed else text}"
    )
    if collapsed:
        _cprint(
            f"\033[90m[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]\033[0m"
        )


def _tui_handle_expand_command(raw: str, assistant_outputs: list[str]) -> bool:
    from .input_loop import parse_expand_target

    target = parse_expand_target(raw)
    if target is None:
        _cprint("用法: /expand [last|编号]")
        return True
    if not assistant_outputs:
        _cprint("当前没有可展开的助手回复。")
        return True
    if target == "last":
        index = len(assistant_outputs)
    else:
        index = int(target)
    if index < 1 or index > len(assistant_outputs):
        _cprint(f"没有编号为 {index} 的助手回复。当前共有 {len(assistant_outputs)} 条。")
        return True
    _cprint(f"===== ASSISTANT RESPONSE #{index} =====")
    _cprint(assistant_outputs[index - 1])
    _cprint("===== END RESPONSE =====")
    return True


def _tui_request_exit(
    shutting_down_ref: list,
    state_lock: threading.Lock,
    is_running_ref: list,
    pending_jobs_ref: list,
    stop_event: threading.Event,
) -> None:
    shutting_down_ref[0] = True
    with state_lock:
        active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
    if active:
        _cprint(f"还有 {active} 个后台任务，等待完成后退出。")
    stop_event.set()


def _tui_handle_command(*, params: TuiHandleCommandParams) -> bool:
    """Handle TUI slash command. Returns True if should exit."""
    import time

    from .input_loop import handle_common_slash_command, is_exit_command

    if is_exit_command(params.user):
        _tui_request_exit(
            params.shutting_down_ref,
            params.state_lock,
            params.is_running_ref,
            params.pending_jobs_ref,
            params.stop_event,
        )
        return True
    if params.user == "/expand" or params.user.startswith("/expand "):
        return _tui_handle_expand_command(params.user, params.assistant_outputs)
    if params.user == "/status":
        with params.state_lock:
            active = params.pending_jobs_ref[0] + (1 if params.is_running_ref[0] else 0)
            prompt = params.running_prompt_ref[0]
            elapsed = time.perf_counter() - params.running_started_at_ref[0] if params.is_running_ref[0] else 0
        if not active:
            _cprint("当前没有后台任务。")
        elif params.is_running_ref[0]:
            _cprint(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {params.pending_jobs_ref[0]} 个任务。")
            _cprint(f"当前任务: {prompt}")
        else:
            _cprint(f"当前没有运行中的任务；队列中还有 {params.pending_jobs_ref[0]} 个任务。")
        if params.use_gateway:
            from ...agent.gateway import render_gateway_status

            for line in render_gateway_status(params.agent, params.paths):
                _cprint(line)
        return True
    return handle_common_slash_command(
        params.user,
        agent=params.agent,
        memory_limit=params.args.memory_limit,
        runtime_inject=params.runtime_inject,
        prompt_files=params.prompt_files,
        print_line=_cprint,
    )


def _tui_enqueue_job(
    user: str,
    show_prompt: bool,
    runtime_inject: list[str],
    prompt_files: list[str],
    jobs,
    state_lock: threading.Lock,
    pending_jobs_ref: list,
) -> None:
    from .fallback_state import ChatJob

    job = ChatJob(
        user=user,
        show_prompt=show_prompt,
        inject=list(runtime_inject),
        prompt_files=list(prompt_files),
    )
    with state_lock:
        pending_jobs_ref[0] += 1
    jobs.put(job)
    _tui_render_user_entry(user)


def _make_worker_config(*, params: WorkerConfigParams) -> TuiWorkerConfig:
    """Build a TuiWorkerConfig from all the passed parameters."""
    return TuiWorkerConfig(
        jobs=params.jobs,
        state_lock=params.state_lock,
        is_running_ref=params.is_running_ref,
        pending_jobs_ref=params.pending_jobs_ref,
        running_prompt_ref=params.running_prompt_ref,
        running_started_at_ref=params.running_started_at_ref,
        agent=params.agent,
        args=params.args,
        paths=params.paths,
        use_gateway=params.use_gateway,
        conversation_history=params.conversation_history,
        history_lock=params.history_lock,
        build_history_context=params.build_history_context,
        assistant_outputs=params.assistant_outputs,
        thinking_line_ref=params.thinking_line_ref,
        stream_buf_ref=params.stream_buf_ref,
        app_ref=params.app_ref,
        last_token_estimate_ref=params.last_token_estimate_ref,
        stop_event=params.stop_event,
    )


def _start_worker_threads(*, params: StartWorkerParams) -> None:
    """Start the TUI worker and refresh threads."""
    worker_cfg = _make_worker_config(
        params=WorkerConfigParams(
            jobs=params.jobs,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            running_prompt_ref=params.running_prompt_ref,
            running_started_at_ref=params.running_started_at_ref,
            agent=params.agent,
            args=params.args,
            paths=params.paths,
            use_gateway=params.use_gateway,
            conversation_history=params.conversation_history,
            history_lock=params.history_lock,
            build_history_context=params.build_history_context,
            assistant_outputs=params.assistant_outputs,
            thinking_line_ref=params.thinking_line_ref,
            stream_buf_ref=params.stream_buf_ref,
            app_ref=params.app_ref,
            last_token_estimate_ref=params.last_token_estimate_ref,
            stop_event=params.stop_event,
        )
    )
    threading.Thread(target=_tui_worker_body, daemon=True, args=(worker_cfg,)).start()
    threading.Thread(target=lambda: _refresh_loop(params.refresh_stop, params.app_ref), daemon=True).start()


def run_tui(*, params: TuiRunParams) -> int:
    from .tui_worker import TuiWorkerConfig, _tui_worker_body

    assistant_outputs: list[str] = []
    thinking_line_ref = [""]
    stop_event = threading.Event()
    stream_buf_ref = [""]
    app_ref: list = [None]
    refresh_stop = threading.Event()

    app = _make_tui_app(
        params=MakeTuiAppParams(
            agent=params.agent,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            running_started_at_ref=params.running_started_at_ref,
            last_token_estimate_ref=params.last_token_estimate_ref,
            jobs=params.jobs,
            pending_jobs_ref_for_enqueue=params.pending_jobs_ref,
            runtime_inject=params.runtime_inject,
            prompt_files=params.prompt_files,
            args=params.args,
            use_gateway=params.use_gateway,
            paths=params.paths,
            assistant_outputs=assistant_outputs,
            shutting_down_ref=params.shutting_down_ref,
            running_prompt_ref=params.running_prompt_ref,
            stop_event=stop_event,
        )
    )
    app_ref[0] = app

    _start_worker_threads(
        params=StartWorkerParams(
            app_ref=app_ref,
            refresh_stop=refresh_stop,
            jobs=params.jobs,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            running_prompt_ref=params.running_prompt_ref,
            running_started_at_ref=params.running_started_at_ref,
            agent=params.agent,
            args=params.args,
            paths=params.paths,
            use_gateway=params.use_gateway,
            conversation_history=params.conversation_history,
            history_lock=params.history_lock,
            build_history_context=params.build_history_context,
            assistant_outputs=assistant_outputs,
            thinking_line_ref=thinking_line_ref,
            stream_buf_ref=stream_buf_ref,
            last_token_estimate_ref=params.last_token_estimate_ref,
            stop_event=stop_event,
        )
    )

    _tui_print_banner(params.agent, params.use_gateway)

    try:
        with patch_stdout():
            app.run()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        refresh_stop.set()

    stop_event.set()
    _cprint("\n再见。")

    params.session_manager.touch_session(params.current_session_id, channel="chat")

    return 0


def _refresh_loop(refresh_stop: threading.Event, app_ref: list) -> None:
    while not refresh_stop.wait(1.0):
        if app_ref[0] is not None:
            app_ref[0].invalidate()


def _make_tui_app(*, params: MakeTuiAppParams):
    """Build and return a prompt_toolkit Application."""
    from .tui_ui_setup import make_tui_app as _make_app

    return _make_app(params=params)


__all__ = [
    "MAX_HISTORY_TURNS",
    "CONTEXT_WINDOW",
    "COLLAPSE_PREVIEW_CHARS",
    "run_tui",
]
