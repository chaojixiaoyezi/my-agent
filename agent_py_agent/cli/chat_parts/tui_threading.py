# LLM: 本模块只启动 TUI worker 与轻量动画刷新线程；业务事件仍由 worker/runtime 发布，刷新线程不得改 reducer 状态。
# 模块用途: 组装后台任务参数，并以 终端交互 接近的帧率驱动 spinner/临时提示重绘。

from __future__ import annotations

import json
import threading
from pathlib import Path

from .tui_params import StartWorkerParams, WorkerConfigParams

TUI_REFRESH_INTERVAL_SECONDS = 0.125
# S-BG1: 后台主代理轮完成通知的监视间隔（gateway 写 conversations/notices/）。
TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS = 20.0


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
    # S-BG1: 后台完成监视线程——子代理完成后的后台自动汇总（gateway 写的
    # notices）即使没有用户操作也要显示到屏幕。
    threading.Thread(
        target=lambda: _background_notice_loop(
            params.stop_event,
            params.agent,
            params.current_session_id,
            params.tui_runtime,
            params.app_ref,
        ),
        daemon=True,
    ).start()


# LLM: 只读 notices 文件并发布 system_message 显示；失败静默跳过（显示增强不能打扰会话）。
# 函数用途: 周期检查当前会话的后台完成通知并显示。
def _background_notice_loop(
    stop_event: threading.Event,
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
) -> None:
    seen: set[float] = set()
    while not stop_event.wait(TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS):
        try:
            _consume_background_notices(agent, session_id, tui_runtime, app_ref, seen)
        except Exception:
            # 监视失败绝不打扰会话；下一轮重试。
            continue


def _consume_background_notices(
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    seen: set[float],
) -> None:
    if tui_runtime is None:
        return
    from ...agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL

    store = getattr(agent, "conversation_store", None)
    if store is None or not session_id:
        return
    root = getattr(store, "root", None)
    if not root:
        return
    thread, _thread_error = store.resolve_thread_report(
        channel=LOCAL_CHAT_CHANNEL,
        channel_conversation_id=session_id,
        channel_user_id=LOCAL_AGENT_USER_ID,
    )
    if thread is None:
        return
    thread_id = str(getattr(thread, "thread_id", "") or "")
    if not thread_id:
        return
    notices_path = Path(root) / "notices" / f"{thread_id}.notices.jsonl"
    if not notices_path.exists():
        return
    fresh: list[dict[str, object]] = []
    for line in notices_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            key = float(row.get("created_at") or 0.0)
        except (TypeError, ValueError):
            key = 0.0
        if key in seen:
            continue
        seen.add(key)
        fresh.append(row)
    if not fresh:
        return
    for row in fresh:
        summary = str(row.get("summary") or "").strip()
        if not summary:
            continue
        notice_text = f"⚙ 后台自动完成：{summary}"
        tui_runtime.publish_background_notice(notice_text, thread_id=thread_id)
    if app_ref[0] is not None:
        app_ref[0].invalidate()


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
