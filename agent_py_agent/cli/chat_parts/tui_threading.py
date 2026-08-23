# LLM: 本模块只启动 TUI worker 与轻量动画刷新线程；业务事件仍由 worker/runtime 发布，刷新线程不得改 reducer 状态。
# 模块用途: 组装后台任务参数，并以 终端交互 接近的帧率驱动 spinner/临时提示重绘。

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path

from .tui_params import StartWorkerParams, WorkerConfigParams

TUI_REFRESH_INTERVAL_SECONDS = 0.125
# S-BG1: 后台主代理轮和子代理面板走 Gateway 轻量快照。会话运行时 用服务端事件推送；
# 当前 HTTP 兼容协议按 1 秒刷新，既保留近实时体验，也不让多个 TUI 高频争抢单 Gateway。
TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS = 1.0
TUI_BACKGROUND_NOTICE_FAILURE_INITIAL_SECONDS = 0.5
TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS = 8.0


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


# LLM: The first snapshot is immediate. Healthy polling stays near-real-time; transport or
# contract failures exponentially back off and reset after the next valid snapshot so disconnected
# TUIs cannot create a reconnect storm against the single Gateway.
# 函数用途: 持续检查当前会话的后台更新；断线时逐步放慢，恢复后自动回到正常刷新速度。
def _background_notice_loop(
    stop_event: threading.Event,
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
) -> None:
    seen: set[float] = set()
    failure_delay = TUI_BACKGROUND_NOTICE_FAILURE_INITIAL_SECONDS
    while not stop_event.is_set():
        try:
            snapshot_ok = _consume_background_notices(
                agent,
                session_id,
                tui_runtime,
                app_ref,
                seen,
            )
        except Exception:
            # 监视失败绝不打扰会话；按同一退避合同等待下一轮。
            snapshot_ok = False
        if snapshot_ok:
            delay = TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS
            failure_delay = TUI_BACKGROUND_NOTICE_FAILURE_INITIAL_SECONDS
        else:
            delay = failure_delay
            failure_delay = min(
                TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS,
                failure_delay * 2.0,
            )
        if stop_event.wait(delay):
            break


# LLM: Each poll reads one canonical thread snapshot. The boolean result distinguishes a valid
# snapshot from transport/contract failure for the caller's backoff; failures preserve the previous
# activity projection, while a valid zero count removes it. Notice rows remain append-only display.
# 函数用途: 消费一次后台状态并返回查询是否成功，供监视线程决定正常刷新还是断线退避。
def _consume_background_notices(
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    seen: set[float],
) -> bool:
    if tui_runtime is None or not session_id:
        return True
    store = getattr(agent, "conversation_store", None)
    if store is None:
        # Gateway 轻量客户端：走 HTTP /client/notices（S-BG1）
        fetcher = getattr(agent, "request_background_notices", None)
        if not callable(fetcher):
            return True
        cursor = max(seen) if seen else 0.0
        try:
            payload = fetcher(session_id, after=cursor)
        except Exception:
            return False
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return False
        activity_changed = (
            _publish_background_activity(
                tui_runtime,
                payload.get("agent_activity")
                if isinstance(payload.get("agent_activity"), dict)
                else {"active_task_count": payload.get("active_task_count")},
            )
            if isinstance(payload.get("agent_activity"), dict)
            or "active_task_count" in payload
            else False
        )
        raw = payload.get("notices")
        if not isinstance(raw, list):
            if activity_changed and app_ref[0] is not None:
                app_ref[0].invalidate()
            return False
        fresh = [dict(row) for row in raw if isinstance(row, dict)]
        for row in fresh:
            _publish_background_notice_row(tui_runtime, row, seen)
        if (fresh or activity_changed) and app_ref[0] is not None:
            app_ref[0].invalidate()
        return True
    from ...agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL

    root = getattr(store, "root", None)
    if not root:
        return True
    thread, _thread_error = store.resolve_thread_report(
        channel=LOCAL_CHAT_CHANNEL,
        channel_conversation_id=session_id,
        channel_user_id=LOCAL_AGENT_USER_ID,
    )
    if thread is None:
        return True
    thread_id = str(getattr(thread, "thread_id", "") or "")
    if not thread_id:
        return True
    from ...agent.conversation.agent_activity import conversation_agent_activity

    activity = conversation_agent_activity(agent, store, thread_id).to_dict()
    activity_changed = _publish_background_activity(
        tui_runtime,
        activity,
    )
    notices_path = Path(root) / "notices" / f"{thread_id}.notices.jsonl"
    if not notices_path.exists():
        if activity_changed and app_ref[0] is not None:
            app_ref[0].invalidate()
        return True
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
        fresh.append(row)
    if not fresh:
        if activity_changed and app_ref[0] is not None:
            app_ref[0].invalidate()
        return True
    for row in fresh:
        _publish_background_notice_row(tui_runtime, row, seen)
    if app_ref[0] is not None:
        app_ref[0].invalidate()
    return True


# LLM: The monitor may project only the typed conversation-agent snapshot built
# from canonical task links and run records. Invalid values leave the current UI
# unchanged instead of guessing activity from notice prose.
# 函数用途: 将会话中的真实主任务与直属子代理状态更新到固定底部区域，并返回画面是否变化。
def _publish_background_activity(
    tui_runtime: object,
    value: object,
) -> bool:
    updater = getattr(tui_runtime, "update_background_activity", None)
    if not callable(updater) or not isinstance(value, Mapping):
        return False
    if value.get("active_task_projection_ok") is False:
        return False
    try:
        count = max(0, int(value.get("active_task_count") or 0))
        compact_count = max(0, int(value.get("compact_count") or 0))
        hidden_count = max(0, int(value.get("hidden_subagent_count") or 0))
    except (TypeError, ValueError):
        return False
    subagents = value.get("subagents")
    if not isinstance(subagents, list | tuple):
        subagents = None
    return bool(
        updater(
            count,
            compact_count=compact_count,
            main_activity=value.get("main_activity"),
            subagents=subagents,
            task_progress_items=value.get("task_progress_items"),
            hidden_subagent_count=hidden_count,
            projection_ok=value.get("subagent_projection_ok") is not False,
            task_progress_projection_ok=(
                value.get("task_progress_projection_ok") is not False
            ),
        )
    )


# LLM: A v2 committed owner delivery becomes an assistant block; legacy v1
# notices remain system hints. Schema fields, not prose, select the display role.
# 函数用途: 去重并发布一条后台回复或旧版后台提示到当前 TUI。
def _publish_background_notice_row(
    tui_runtime: object,
    row: dict[str, object],
    seen: set[float],
) -> None:
    summary = str(row.get("summary") or "").strip()
    content = str(row.get("content") or summary).strip()
    thread_id = str(row.get("thread_id") or "")
    if not content:
        return
    try:
        key = float(row.get("created_at") or 0.0)
    except (TypeError, ValueError):
        key = 0.0
    if key in seen:
        return
    seen.add(key)
    progress_items = row.get("task_progress_items")
    progress_publisher = getattr(tui_runtime, "publish_task_progress_snapshot", None)
    if isinstance(progress_items, list | tuple) and callable(progress_publisher):
        progress_publisher(progress_items)
    if str(row.get("display_kind") or "") == "assistant_response":
        publisher = getattr(tui_runtime, "publish_background_response", None)
        if callable(publisher):
            publisher(content, thread_id=thread_id)
        return
    notice_text = f"⚙ 后台更新：{summary}"
    tui_runtime.publish_background_notice(notice_text, thread_id=thread_id)


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
