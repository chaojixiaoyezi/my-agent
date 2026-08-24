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
            params.agent_navigation,
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
            params.agent_navigation,
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
    agent_navigation: object | None = None,
) -> None:
    seen: set[float] = set()
    event_cursor = [0]
    failure_delay = TUI_BACKGROUND_NOTICE_FAILURE_INITIAL_SECONDS
    while not stop_event.is_set():
        try:
            snapshot_ok = _consume_background_notices(
                agent,
                session_id,
                tui_runtime,
                app_ref,
                seen,
                event_cursor,
                agent_navigation,
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
    event_cursor_ref: list[int] | None = None,
    agent_navigation: object | None = None,
) -> bool:
    if tui_runtime is None or not session_id:
        return True
    event_cursor = event_cursor_ref if event_cursor_ref is not None else [0]
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return _consume_gateway_background_snapshot(
            agent,
            session_id,
            tui_runtime,
            app_ref,
            seen,
            event_cursor,
            agent_navigation,
        )
    return _consume_local_background_snapshot(
        agent,
        store,
        session_id,
        tui_runtime,
        app_ref,
        seen,
        event_cursor,
        agent_navigation,
    )


# LLM: Thin clients consume the one authenticated /client/notices snapshot.
# Notification time and transcript integer cursors remain independent, and any
# invalid response is returned to the caller's existing exponential backoff.
# 函数用途: 从单 Gateway 拉取一次后台活动、过程事件和最终回复并刷新 TUI。
def _consume_gateway_background_snapshot(
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    seen: set[float],
    event_cursor: list[int],
    agent_navigation: object | None = None,
) -> bool:
    fetcher = getattr(agent, "request_background_notices", None)
    if not callable(fetcher):
        return True
    try:
        payload = fetcher(
            session_id,
            after=max(seen) if seen else 0.0,
            event_after=max(0, int(event_cursor[0] or 0)),
        )
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
            agent_navigation=agent_navigation,
        )
        if isinstance(payload.get("agent_activity"), dict)
        or "active_task_count" in payload
        else False
    )
    transcript_ok, transcript_changed = _consume_background_transcript_projection(
        tui_runtime,
        payload.get("transcript_events"),
        payload.get("event_cursor"),
        event_cursor,
    )
    if not transcript_ok:
        return False
    raw = payload.get("notices")
    if not isinstance(raw, list):
        return False
    fresh = [dict(row) for row in raw if isinstance(row, dict)]
    for row in fresh:
        _publish_background_notice_row(tui_runtime, row, seen)
    agent_view_ok, agent_view_changed = _consume_selected_agent_view(
        agent,
        None,
        session_id,
        agent_navigation,
    )
    if not agent_view_ok:
        return False
    if (
        fresh or activity_changed or transcript_changed or agent_view_changed
    ) and app_ref[0] is not None:
        app_ref[0].invalidate()
    return True


# LLM: Embedded/local mode reads the same agent-owned projection and event ring
# without HTTP. ConversationStore and notice files remain authoritative for
# thread resolution and final replies; the ring is still display-only.
# 函数用途: 在本地完整 Agent 模式消费一次后台活动、过程事件和最终通知。
def _consume_local_background_snapshot(
    agent: object,
    store: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    seen: set[float],
    event_cursor: list[int],
    agent_navigation: object | None = None,
) -> bool:
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
        agent_navigation=agent_navigation,
    )
    from ...agent.conversation.background_transcript import (
        read_background_transcript_events,
    )

    transcript = read_background_transcript_events(
        agent,
        thread_id=thread_id,
        after=max(0, int(event_cursor[0] or 0)),
    )
    transcript_ok, transcript_changed = _consume_background_transcript_projection(
        tui_runtime,
        transcript.get("events"),
        transcript.get("cursor"),
        event_cursor,
    )
    if not transcript_ok:
        return False
    notices_path = Path(root) / "notices" / f"{thread_id}.notices.jsonl"
    agent_view_ok, agent_view_changed = _consume_selected_agent_view(
        agent,
        store,
        session_id,
        agent_navigation,
    )
    if not agent_view_ok:
        return False
    if not notices_path.exists():
        if (
            activity_changed or transcript_changed or agent_view_changed
        ) and app_ref[0] is not None:
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
        if (
            activity_changed or transcript_changed or agent_view_changed
        ) and app_ref[0] is not None:
            app_ref[0].invalidate()
        return True
    for row in fresh:
        _publish_background_notice_row(tui_runtime, row, seen)
    if app_ref[0] is not None:
        app_ref[0].invalidate()
    return True


# LLM: The TUI cursor advances only through the typed background event page.
# Invalid transport shape triggers the existing poll backoff; event prose is
# never parsed, and the runtime remains the only sequencer/reducer owner.
# 函数用途: 消费一页后台过程事件并推进独立整数游标，返回协议是否有效及画面是否变化。
def _consume_background_transcript_projection(
    tui_runtime: object,
    events: object,
    cursor_value: object,
    cursor_ref: list[int],
) -> tuple[bool, bool]:
    if not isinstance(events, list):
        return False, False
    try:
        response_cursor = max(0, int(cursor_value or 0))
    except (TypeError, ValueError):
        return False, False
    publisher = getattr(tui_runtime, "publish_background_transcript_events", None)
    if events and not callable(publisher):
        return False, False
    try:
        consumed_cursor = int(publisher(events) or 0) if callable(publisher) else 0
    except (TypeError, ValueError):
        return False, False
    cursor_ref[0] = max(cursor_ref[0], response_cursor, consumed_cursor)
    return True, bool(events)


# LLM: The monitor may project only the typed conversation-agent snapshot built
# from canonical task links and run records. Invalid values leave the current UI
# unchanged instead of guessing activity from notice prose.
# 函数用途: 将会话中的真实主任务与直属子代理状态更新到固定底部区域，并返回画面是否变化。
def _publish_background_activity(
    tui_runtime: object,
    value: object,
    *,
    agent_navigation: object | None = None,
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
    changed = bool(
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
    row_updater = getattr(agent_navigation, "update_rows", None)
    if subagents is not None and callable(row_updater):
        changed = bool(row_updater("", subagents)) or changed
    return changed


# LLM: The selected child page is polled only while its exact run id is active.
# Gateway and embedded modes feed the same navigation projection, and failures
# preserve the prior page for the caller's existing exponential backoff.
# 函数用途: 拉取当前进入的子代理详情页并增量应用到独立 TUI runtime。
def _consume_selected_agent_view(
    agent: object,
    store: object | None,
    session_id: str,
    agent_navigation: object | None,
) -> tuple[bool, bool]:
    snapshotter = getattr(agent_navigation, "snapshot", None)
    applier = getattr(agent_navigation, "apply_agent_view", None)
    cursor_reader = getattr(agent_navigation, "event_cursor", None)
    if not callable(snapshotter) or not callable(applier) or not callable(cursor_reader):
        return True, False
    snapshot = snapshotter()
    run_id = str(getattr(snapshot, "active_run_id", "") or "").strip()
    if not run_id:
        return True, False
    cursor = max(0, int(cursor_reader(run_id) or 0))
    try:
        if store is None:
            fetcher = getattr(agent, "request_agent_view", None)
            if not callable(fetcher):
                return False, False
            payload = fetcher(
                session_id,
                run_id=run_id,
                event_after=cursor,
            )
        else:
            from ...agent.conversation.agent_activity import conversation_agent_view

            payload = conversation_agent_view(
                agent,
                store,
                run_id,
                after=cursor,
            )
    except Exception:
        return False, False
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return False, False
    return True, bool(applier(run_id, payload))


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
    agent_navigation: object | None = None,
) -> None:
    while not refresh_stop.wait(TUI_REFRESH_INTERVAL_SECONDS):
        active_runtime = tui_runtime
        runtime_reader = getattr(agent_navigation, "active_runtime", None)
        if callable(runtime_reader):
            active_runtime = runtime_reader()
        if (
            app_ref[0] is not None
            and active_runtime.needs_periodic_refresh()
        ):
            app_ref[0].invalidate()


__all__ = ["_refresh_loop", "_start_worker_threads"]
