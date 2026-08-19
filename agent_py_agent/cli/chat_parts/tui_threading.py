# LLM: 本模块只启动 TUI worker 与轻量动画刷新线程；业务事件仍由 worker/runtime 发布，刷新线程不得改 reducer 状态。
# 模块用途: 组装后台任务参数，并以 终端交互 接近的帧率驱动 spinner/临时提示重绘。

from __future__ import annotations

import threading

from .tui_params import StartWorkerParams, WorkerConfigParams

TUI_REFRESH_INTERVAL_SECONDS = 0.125


# LLM: config factory 只能透传同一 queue/refs/runtime；不得在这里复制状态或创建第二个 TuiRuntime。
# 函数用途: 将线程启动参数收窄成 worker 配置。
def _make_worker_config(*, params: WorkerConfigParams):
    from .tui_worker import TuiWorkerConfig

    return TuiWorkerConfig(
        jobs=params.jobs,
        state_lock=params.state_lock,
        is_running_ref=params.is_running_ref,
        pending_jobs_ref=params.pending_jobs_ref,
        running_prompt_ref=params.running_prompt_ref,
        running_request_id_ref=params.running_request_id_ref,
        running_started_at_ref=params.running_started_at_ref,
        agent=params.agent,
        args=params.args,
        paths=params.paths,
        use_gateway=params.use_gateway,
        conversation_history=params.conversation_history,
        history_lock=params.history_lock,
        build_history_context=params.build_history_context,
        assistant_outputs=params.assistant_outputs,
        last_token_estimate_ref=params.last_token_estimate_ref,
        stop_event=params.stop_event,
        current_session_id=params.current_session_id,
        tui_runtime=params.tui_runtime,
    )


# LLM: 两个 daemon thread 分别执行 canonical prompt queue 与纯重绘 tick；应用退出由共享 stop events 收口。
# 函数用途: 启动 TUI 后台 worker 和动画刷新线程。
def _start_worker_threads(*, params: StartWorkerParams) -> None:
    from .tui_worker import _tui_worker_body

    worker_cfg = _make_worker_config(
        params=WorkerConfigParams(
            jobs=params.jobs,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            running_prompt_ref=params.running_prompt_ref,
            running_request_id_ref=params.running_request_id_ref,
            running_started_at_ref=params.running_started_at_ref,
            agent=params.agent,
            args=params.args,
            paths=params.paths,
            use_gateway=params.use_gateway,
            conversation_history=params.conversation_history,
            history_lock=params.history_lock,
            build_history_context=params.build_history_context,
            assistant_outputs=params.assistant_outputs,
            last_token_estimate_ref=params.last_token_estimate_ref,
            stop_event=params.stop_event,
            current_session_id=params.current_session_id,
            tui_runtime=params.tui_runtime,
        )
    )
    threading.Thread(target=_tui_worker_body, daemon=True, args=(worker_cfg,)).start()
    threading.Thread(
        target=lambda: _refresh_loop(
            params.refresh_stop,
            params.app_ref,
            params.tui_runtime,
        ),
        daemon=True,
    ).start()


# LLM: refresh loop 只在 runtime 报告存在可见动画/短提示时 invalidate；typed event 自带 redraw，空闲时必须零周期整屏重绘。
# 函数用途: 在应用存活期间按需驱动 spinner 和短提示，不让长历史在空闲时持续占用 CPU。
def _refresh_loop(
    refresh_stop: threading.Event,
    app_ref: list,
    tui_runtime: object,
) -> None:
    while not refresh_stop.wait(TUI_REFRESH_INTERVAL_SECONDS):
        if (
            app_ref[0] is not None
            and tui_runtime.needs_periodic_refresh()
        ):
            app_ref[0].invalidate()


__all__ = ["_refresh_loop", "_start_worker_threads"]
