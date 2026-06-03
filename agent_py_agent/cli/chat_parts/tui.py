
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .rendering import _cprint, _tui_print_banner, progress_bar
from .tui_activity import format_activity_text
from .tui_params import (
    MakeTuiAppParams,
    StartWorkerParams,
    TuiHandleCommandParams,
    TuiRunParams,
)
from .tui_threading import _start_worker_threads

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:  # pragma: no cover
    Application = None
    patch_stdout = None


@dataclass
class TuiInputRefs:
    app_ref: list
    refresh_stop: threading.Event
    assistant_outputs: list[str]
    thinking_line_ref: list[str]
    stream_buf_ref: list[str]
    stream_visible_text_ref: list[str]
    stop_event: threading.Event


@dataclass
class TuiStatusRefs:
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_started_at_ref: list
    last_token_estimate_ref: list
    thinking_line_ref: list


@dataclass
class TuiExitRefs:
    shutting_down_ref: list
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    stop_event: threading.Event


@dataclass
class TuiLoopContext:
    app: object
    refresh_stop: threading.Event
    stop_event: threading.Event
    session_manager: object
    current_session_id: str


CONTEXT_WINDOW = 200_000
COLLAPSE_PREVIEW_CHARS = 900


def _tui_get_status_text(
    refs: TuiStatusRefs,
    model: str,
    *,
    context_window_chars: int = CONTEXT_WINDOW,
) -> str:
    with refs.state_lock:
        tokens = refs.last_token_estimate_ref[0]
    window = max(1, int(context_window_chars or CONTEXT_WINDOW))
    pct = tokens / window if window else 0
    bar = progress_bar(pct)
    pieces = [
        f"model {model}",
        f"ctx {tokens / 1000:.1f}K/{window / 1000:.0f}K",
        f"[{bar}] {pct:.0%}",
    ]
    return " | ".join(pieces)


def _tui_get_activity_text(refs: TuiStatusRefs) -> str:
    with refs.state_lock:
        thinking = refs.thinking_line_ref[0] if refs.thinking_line_ref else ""
        started_at = refs.running_started_at_ref[0]
        running = bool(refs.is_running_ref[0])
    if not thinking:
        return ""
    return format_activity_text(thinking, started_at, running, now=time.perf_counter())


def _tui_handle_expand_command(raw: str, assistant_outputs: list[str]) -> bool:
    from .input_loop import parse_expand_target

    target = parse_expand_target(raw)
    if target is None:
        _cprint("Usage: /expand [last|number]")
        return True
    if not assistant_outputs:
        _cprint("No assistant responses are available to expand.")
        return True
    index = len(assistant_outputs) if target == "last" else int(target)
    if index < 1 or index > len(assistant_outputs):
        _cprint(f"No assistant response #{index}; current count is {len(assistant_outputs)}.")
        return True
    _cprint(f"===== ASSISTANT RESPONSE #{index} =====")
    _cprint(assistant_outputs[index - 1])
    _cprint("===== END RESPONSE =====")
    return True


def _tui_request_exit(refs: TuiExitRefs) -> None:
    refs.shutting_down_ref[0] = True
    with refs.state_lock:
        active = refs.pending_jobs_ref[0] + (1 if refs.is_running_ref[0] else 0)
    if active:
        _cprint(f"Waiting for {active} background task(s) before exit.")
    refs.stop_event.set()


def _make_tui_exit_refs(params: TuiHandleCommandParams) -> TuiExitRefs:
    return TuiExitRefs(
        shutting_down_ref=params.shutting_down_ref,
        state_lock=params.state_lock,
        is_running_ref=params.is_running_ref,
        pending_jobs_ref=params.pending_jobs_ref,
        stop_event=params.stop_event,
    )


def _show_tui_status(params: TuiHandleCommandParams) -> None:
    import time

    with params.state_lock:
        active = params.pending_jobs_ref[0] + (1 if params.is_running_ref[0] else 0)
        prompt = params.running_prompt_ref[0]
        elapsed = (
            time.perf_counter() - params.running_started_at_ref[0]
            if params.is_running_ref[0]
            else 0
        )
    if not active:
        _cprint("No background task is running.")
    elif params.is_running_ref[0]:
        _cprint(
            f"Responding for {elapsed:.0f}s; {params.pending_jobs_ref[0]} queued task(s)."
        )
        _cprint(f"Current task: {prompt}")
    else:
        _cprint(f"No active task; {params.pending_jobs_ref[0]} queued task(s).")
    if params.use_gateway:
        from ...agent.gateway import render_gateway_status

        for line in render_gateway_status(params.agent, params.paths):
            _cprint(line)


def _tui_handle_command(*, params: TuiHandleCommandParams) -> bool:
    from .input_loop import handle_common_slash_command, is_exit_command
    from .slash_command_types import SlashCommandContext

    if is_exit_command(params.user):
        _tui_request_exit(_make_tui_exit_refs(params))
        return True
    if params.user == "/expand" or params.user.startswith("/expand "):
        return _tui_handle_expand_command(params.user, params.assistant_outputs)
    if params.user == "/status":
        _show_tui_status(params)
        return True
    return handle_common_slash_command(
        params.user,
        ctx=SlashCommandContext(
            agent=params.agent,
            memory_limit=params.args.memory_limit,
            runtime_inject=params.runtime_inject,
            prompt_files=params.prompt_files,
            print_line=_cprint,
        ),
    )


def _run_tui_loop(ctx: TuiLoopContext) -> None:
    from .rendering import set_tui_output_sink, set_tui_stream_sink

    try:
        with patch_stdout():
            ctx.app.run()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        set_tui_output_sink(None)
        set_tui_stream_sink(None)
        ctx.refresh_stop.set()
    ctx.stop_event.set()
    _cprint("\nGoodbye.")
    ctx.session_manager.touch_session(ctx.current_session_id, channel="chat")


def _make_tui_app_params(
    run_config: TuiRunParams,
    assistant_outputs: list[str],
    thinking_line_ref: list[str],
    stop_event: threading.Event,
) -> MakeTuiAppParams:
    return MakeTuiAppParams(
        agent=run_config.agent,
        state_lock=run_config.state_lock,
        is_running_ref=run_config.is_running_ref,
        pending_jobs_ref=run_config.pending_jobs_ref,
        running_started_at_ref=run_config.running_started_at_ref,
        last_token_estimate_ref=run_config.last_token_estimate_ref,
        jobs=run_config.jobs,
        pending_jobs_ref_for_enqueue=run_config.pending_jobs_ref,
        thinking_line_ref=thinking_line_ref,
        runtime_inject=run_config.runtime_inject,
        prompt_files=run_config.prompt_files,
        args=run_config.args,
        use_gateway=run_config.use_gateway,
        paths=run_config.paths,
        assistant_outputs=assistant_outputs,
        shutting_down_ref=run_config.shutting_down_ref,
        running_prompt_ref=run_config.running_prompt_ref,
        stop_event=stop_event,
    )


def _make_start_worker_params(
    params: TuiRunParams,
    refs: TuiInputRefs,
) -> StartWorkerParams:
    return StartWorkerParams(
        app_ref=refs.app_ref,
        refresh_stop=refs.refresh_stop,
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
        assistant_outputs=refs.assistant_outputs,
        thinking_line_ref=refs.thinking_line_ref,
        stream_buf_ref=refs.stream_buf_ref,
        stream_visible_text_ref=refs.stream_visible_text_ref,
        last_token_estimate_ref=params.last_token_estimate_ref,
        stop_event=refs.stop_event,
    )


def run_tui(*, params: TuiRunParams) -> int:
    assistant_outputs: list[str] = []
    thinking_line_ref = [""]
    stop_event = threading.Event()
    stream_buf_ref = [""]
    stream_visible_text_ref = [""]
    app_ref: list = [None]
    refresh_stop = threading.Event()

    app = _make_tui_app(params=_make_tui_app_params(params, assistant_outputs, thinking_line_ref, stop_event))
    app_ref[0] = app
    refs = TuiInputRefs(
        app_ref=app_ref,
        refresh_stop=refresh_stop,
        assistant_outputs=assistant_outputs,
        thinking_line_ref=thinking_line_ref,
        stream_buf_ref=stream_buf_ref,
        stream_visible_text_ref=stream_visible_text_ref,
        stop_event=stop_event,
    )
    _start_worker_threads(params=_make_start_worker_params(params, refs))
    _tui_print_banner(params.agent, params.use_gateway)
    _run_tui_loop(
        TuiLoopContext(
            app=app,
            refresh_stop=refresh_stop,
            stop_event=stop_event,
            session_manager=params.session_manager,
            current_session_id=params.current_session_id,
        )
    )
    return 0


def _make_tui_app(*, params: MakeTuiAppParams):
    from .tui_ui_setup import make_tui_app as _make_app

    return _make_app(params=params)


__all__ = [
    "CONTEXT_WINDOW",
    "COLLAPSE_PREVIEW_CHARS",
    "TuiExitRefs",
    "TuiStatusRefs",
    "run_tui",
]
