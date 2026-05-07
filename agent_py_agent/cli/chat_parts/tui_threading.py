# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

import threading

from .tui_params import StartWorkerParams, WorkerConfigParams


# LLM: _make_worker_config 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _make_worker_config(*, params: WorkerConfigParams):
    from .tui_worker import TuiWorkerConfig

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
        stream_visible_text_ref=params.stream_visible_text_ref,
        app_ref=params.app_ref,
        last_token_estimate_ref=params.last_token_estimate_ref,
        stop_event=params.stop_event,
    )


# LLM: _start_worker_threads 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _start_worker_threads(*, params: StartWorkerParams) -> None:
    from .tui_worker import _tui_worker_body

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
            stream_visible_text_ref=params.stream_visible_text_ref,
            app_ref=params.app_ref,
            last_token_estimate_ref=params.last_token_estimate_ref,
            stop_event=params.stop_event,
        )
    )
    threading.Thread(target=_tui_worker_body, daemon=True, args=(worker_cfg,)).start()
    threading.Thread(
        target=lambda: _refresh_loop(params.refresh_stop, params.app_ref),
        daemon=True,
    ).start()


# LLM: _refresh_loop 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _refresh_loop(refresh_stop: threading.Event, app_ref: list) -> None:
    while not refresh_stop.wait(1.0):
        if app_ref[0] is not None:
            app_ref[0].invalidate()


__all__ = ["_start_worker_threads"]
