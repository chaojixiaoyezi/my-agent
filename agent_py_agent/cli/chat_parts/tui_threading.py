# LLM: 本模块只启动 TUI worker 与轻量动画刷新线程；业务事件仍由 worker/runtime 发布，刷新线程不得改 reducer 状态。
# 过程轮询按流身份与序号一起确认；换进程只重基易失游标，不重置 canonical 消息位置。
# 模块用途: 组装后台任务参数，驱动 spinner/提示重绘；后台读取失败显式显示，不能假装状态仍实时。

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path

from .tui_params import StartWorkerParams, WorkerConfigParams

TUI_REFRESH_INTERVAL_SECONDS = 0.125
# S-BG1: 后台主代理轮和子代理面板走 Gateway 轻量快照。会话运行时 用服务端事件推送；
# 当前 HTTP 兼容协议在有任务时按 1 秒刷新，完全空闲时降到 5 秒，兼顾近实时与单 Gateway 负载。
TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS = 1.0
TUI_BACKGROUND_NOTICE_IDLE_INTERVAL_SECONDS = 5.0
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
    # 后台完成仍读同一 canonical 消息流，无需用户操作，也不另存回复正文。
    threading.Thread(
        target=lambda: _background_notice_loop(
            params.stop_event,
            params.agent,
            params.current_session_id,
            params.tui_runtime,
            params.app_ref,
            params.agent_navigation,
            params.is_running_ref,
        ),
        daemon=True,
    ).start()


# LLM: The first snapshot is immediate. Foreground/background-active sessions poll at one second;
# inactive sessions poll at five seconds. Transport failures use a separate exponential backoff
# that resets after the next valid snapshot, so detached TUIs cannot storm the single Gateway.
# 函数用途: 持续检查当前会话更新；失败沿原退避节奏重试并标记状态未同步，不终止后台任务。
def _background_notice_loop(
    stop_event: threading.Event,
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    agent_navigation: object | None = None,
    foreground_running_ref: list[bool] | None = None,
) -> None:
    seen: set[tuple[str, str]] = set()
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
            # 监视失败不改业务状态；显示旧快照警告后沿原退避合同等待下一轮。
            snapshot_ok = False
        health_publisher = getattr(tui_runtime, "publish_background_sync_status", None)
        if callable(health_publisher) and health_publisher(ok=snapshot_ok):
            if app_ref and app_ref[0] is not None:
                app_ref[0].invalidate()
        if snapshot_ok:
            foreground_running = bool(
                foreground_running_ref and foreground_running_ref[0]
            )
            activity_reader = getattr(
                tui_runtime,
                "has_active_background_task",
                None,
            )
            background_active = bool(activity_reader()) if callable(activity_reader) else False
            delay = (
                TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS
                if foreground_running or background_active
                else TUI_BACKGROUND_NOTICE_IDLE_INTERVAL_SECONDS
            )
            failure_delay = TUI_BACKGROUND_NOTICE_FAILURE_INITIAL_SECONDS
        else:
            delay = failure_delay
            failure_delay = min(
                TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS,
                failure_delay * 2.0,
            )
        if stop_event.wait(delay):
            break


# LLM: Each poll reads one canonical thread snapshot. The boolean result distinguishes transport
# health; the runtime-owned typed activity controller separately exposes the healthy idle cadence.
# Failures preserve the previous projection; a valid zero remains authoritative display state.
# 函数用途: 消费一次后台快照并返回连接状态；已发布的活动计数供下一轮调速。
def _consume_background_notices(
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    seen: set[tuple[str, str]],
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


# LLM: Thin clients consume the authenticated canonical message page plus an independent
# process-event cursor paired with its stream identity. Only a new stream can reset the event
# sequence; canonical message offsets never rewind. Typed activity controls poll cadence.
# Invalid responses enter the existing exponential backoff without clearing display state.
# 函数用途: 先按 canonical 顺序收用户/完整消息，再补未覆盖的易失过程；避免思考先于用户输入或 final 重复。
def _consume_gateway_background_snapshot(
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    seen: set[tuple[str, str]],
    event_cursor: list[int],
    agent_navigation: object | None = None,
) -> bool:
    fetcher = getattr(agent, "request_background_notices", None)
    if not callable(fetcher):
        return True
    message_after = max(0, int(getattr(tui_runtime, "background_message_cursor", 0)))
    try:
        payload = fetcher(
            session_id,
            after=message_after,
            event_after=max(0, int(event_cursor[0] or 0)),
            event_stream_id=str(getattr(tui_runtime, "background_event_stream_id", "")),
        )
    except Exception:
        return False
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    permission_changed = _sync_gateway_agent_permissions(
        agent,
        session_id,
        tui_runtime,
        payload.get("agent_permission_requests", []),
    )
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
    raw = payload.get("notices")
    cursor = payload.get("cursor")
    if not isinstance(raw, list) or type(cursor) is not int or cursor < message_after:
        return False
    if any(not isinstance(row, dict) for row in raw):
        return False
    fresh = [dict(row) for row in raw]
    for row in fresh:
        if not _publish_background_notice_row(tui_runtime, row, seen):
            return False
    transcript_ok, transcript_changed = _consume_background_transcript_projection(
        tui_runtime,
        payload.get("transcript_events"),
        payload.get("event_cursor"),
        event_cursor,
        stream_id=payload.get("event_stream_id"),
    )
    if not transcript_ok:
        return False
    tui_runtime.background_message_cursor = cursor
    agent_view_ok, agent_view_changed = _consume_selected_agent_view(
        agent,
        None,
        session_id,
        agent_navigation,
    )
    if not agent_view_ok:
        return False
    if (
        fresh
        or activity_changed
        or transcript_changed
        or agent_view_changed
        or permission_changed
    ) and app_ref[0] is not None:
        app_ref[0].invalidate()
    return True


# LLM: Embedded/local mode reads the same agent-owned projection and event ring without HTTP.
# ConversationStore is the only message source; the typed active count affects only the
# next display delay, while the event ring stays display-only and uses the same stream handshake.
# 函数用途: 本地模式共用canonical过程检查点与后台活动流；只推进成功显示的游标，不改变模型输入。
def _consume_local_background_snapshot(
    agent: object,
    store: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    seen: set[tuple[str, str]],
    event_cursor: list[int],
    agent_navigation: object | None = None,
) -> bool:
    from ...agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL

    root = getattr(getattr(store, "storage", None), "root", None)
    if not root:
        return True
    thread, _thread_error = store.threads.resolve_report(
        channel=LOCAL_CHAT_CHANNEL,
        channel_conversation_id=session_id,
        channel_user_id=LOCAL_AGENT_USER_ID,
    )
    if thread is None:
        return True
    thread_id = str(getattr(thread, "thread_id", "") or "")
    if not thread_id:
        return True
    permission_changed = _sync_local_agent_permissions(
        agent,
        thread,
        tui_runtime,
    )
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
        stream_id=str(getattr(tui_runtime, "background_event_stream_id", "")),
    )
    transcript_ok, transcript_changed = _consume_background_transcript_projection(
        tui_runtime,
        transcript.get("events"),
        transcript.get("cursor"),
        event_cursor,
        stream_id=transcript.get("stream_id"),
    )
    if not transcript_ok:
        return False
    from ...agent.conversation.message_stream import read_background_response_page

    fresh, cursor, messages_ok = read_background_response_page(
        store, thread_id, after=max(0, int(getattr(tui_runtime, "background_message_cursor", 0))),
        include_display_checkpoints=True,
    )
    if not messages_ok:
        return False
    agent_view_ok, agent_view_changed = _consume_selected_agent_view(
        agent,
        store,
        session_id,
        agent_navigation,
    )
    if not agent_view_ok:
        return False
    if not fresh:
        tui_runtime.background_message_cursor = cursor
        if (
            activity_changed
            or transcript_changed
            or agent_view_changed
            or permission_changed
        ) and app_ref[0] is not None:
            app_ref[0].invalidate()
        return True
    for row in fresh:
        if not _publish_background_notice_row(tui_runtime, row, seen):
            return False
    tui_runtime.background_message_cursor = cursor
    if app_ref[0] is not None:
        app_ref[0].invalidate()
    return True




# LLM: Gateway 行已绑定 owner；适配器给主/子审批同一精确 POST 回写，未确认的响应保留面板，不能当成批准。
# 函数用途: 将单 Gateway 返回的主/子后台审批接进 TUI，并回写用户选择。
def _sync_gateway_agent_permissions(
    agent: object,
    session_id: str,
    tui_runtime: object,
    value: object,
) -> bool:
    syncer = getattr(tui_runtime, "sync_agent_permission_requests", None)
    sender = getattr(agent, "request_agent_permission", None)
    if not callable(syncer) or not callable(sender):
        return False

    # LLM: 闭包保留 canonical run/request，只向原代理审批端点提交 typed 决定，标题不能改变请求。
    # 函数用途: 回写当前面板选中的精确主/子后台审批决定。
    def write_decision(run_id, request, decision):
        result = sender(
            session_id,
            run_id=run_id,
            request=request.to_dict(),
            decision=decision.to_dict(),
        )
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            notifier = getattr(tui_runtime, "set_notice", None)
            if callable(notifier):
                notifier("审批结果尚未写回，请重试", duration_seconds=2.5)
        return result

    return bool(syncer(value, decision_writer=write_decision))


# LLM: 嵌入模式与 Gateway 复用同一审批桥和完整请求校验；consumer 续租仅表明界面在线，不构成批准。
# 函数用途: 在不走 HTTP 的完整 Agent TUI 中同步并处理主/子后台审批。
def _sync_local_agent_permissions(
    agent: object,
    thread: object,
    tui_runtime: object,
) -> bool:
    syncer = getattr(tui_runtime, "sync_agent_permission_requests", None)
    root_task_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    if not callable(syncer) or not root_task_id:
        return False
    from ...agent.conversation.agent_tool_approval import (
        list_pending_agent_tool_approvals,
        renew_agent_tool_approval_consumer,
        resolve_agent_tool_approval,
    )

    try:
        renew_agent_tool_approval_consumer(agent, root_task_id=root_task_id)
        rows = list_pending_agent_tool_approvals(
            agent,
            root_task_id=root_task_id,
        )
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        return False

    # LLM: Local resolution still validates the full canonical request under
    # the process-shared transition lock before changing pending state.
    # 函数用途: 将嵌入式 TUI 的选择写回同一审批账本。
    def write_decision(run_id, request, decision):
        return resolve_agent_tool_approval(
            agent,
            run_id=run_id,
            request_value=request,
            decision_value=decision,
        )

    return bool(syncer(rows, decision_writer=write_decision))


# LLM: A stream change rebases only the volatile cursor after successful publication; existing
# blocks, input receipts, canonical messages and task state remain untouched. Invalid shape or
# same-stream cursor regression uses the existing backoff, never content/time-based guessing.
# 函数用途: 消费带身份的后台过程页；同流递增、换流重新接续，发布失败不确认新游标。
def _consume_background_transcript_projection(
    tui_runtime: object,
    events: object,
    cursor_value: object,
    cursor_ref: list[int],
    *,
    stream_id: object,
) -> tuple[bool, bool]:
    if (
        not isinstance(events, list)
        or not isinstance(stream_id, str)
        or type(cursor_value) is not int
        or cursor_value < 0
        or (events and not stream_id)
    ):
        return False, False
    response_cursor = cursor_value
    previous_stream = str(getattr(tui_runtime, "background_event_stream_id", ""))
    if stream_id == previous_stream and response_cursor < cursor_ref[0]:
        return False, False
    if not stream_id and (previous_stream or response_cursor != cursor_ref[0]):
        return False, False
    publisher = getattr(tui_runtime, "publish_background_transcript_events", None)
    if events and not callable(publisher):
        return False, False
    try:
        consumed_cursor = int(publisher(events) or 0) if callable(publisher) else 0
    except (TypeError, ValueError):
        return False, False
    if consumed_cursor > response_cursor:
        return False, False
    cursor_ref[0] = response_cursor
    tui_runtime.background_event_stream_id = stream_id
    return True, bool(events)


# LLM: The monitor may project only the typed conversation-agent snapshot built
# from canonical task links and run records. Invalid values leave the current UI
# unchanged instead of guessing activity from notice prose.
# 函数用途: 更新主任务、子代理与独立上下文数字；数字读取失败保留旧帧，不能把未知当清空。
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
    goals = value.get("goals")
    if not isinstance(goals, list | tuple):
        goals = None
    progress_items = value.get("task_progress_items")
    task_progress = (
        {
            "items": progress_items,
            "generation_id": value.get("task_progress_generation_id"),
            "plan_revision": value.get("task_progress_plan_revision"),
        }
        if isinstance(progress_items, list | tuple)
        else None
    )
    changed = bool(
        updater(
            count,
            {
                "compact_count": compact_count,
                "main_activity": value.get("main_activity"),
                "context_usage": value.get("context_usage") if value.get("context_usage_projection_ok") is not False else None,
                "model_metrics": value.get("model_metrics"),
                "goals": goals,
                "subagents": subagents,
                "task_progress": task_progress,
                "hidden_subagent_count": hidden_count,
                "projection_ok": value.get("subagent_projection_ok") is not False,
                "goal_projection_ok": value.get("goal_projection_ok") is not False,
                "task_progress_projection_ok": (
                    value.get("task_progress_projection_ok") is not False
                ),
            },
        )
    )
    row_updater = getattr(agent_navigation, "update_rows", None)
    goal_updater = getattr(agent_navigation, "update_goal_rows", None)
    if goals is not None and callable(goal_updater):
        changed = bool(goal_updater(goals)) or changed
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


# LLM: user/process必须带canonical显示事件；process没有正文，发布后才确认消息游标，不改变运行或审批。
# 函数用途: 发布已提交输入、回复及完整过程块；同源副本去重，保存但未final的过程也可显示。
def _publish_background_notice_row(
    tui_runtime: object,
    row: dict[str, object],
    seen: set[tuple[str, str]],
) -> bool:
    content = str(row.get("content") or "").strip()
    thread_id = str(row.get("thread_id") or "")
    message_id = str(row.get("message_id") or "")
    if (
        row.get("schema_version") != "background_message.v1"
        or row.get("display_kind") not in {"assistant_response", "user_message", "process_event"}
        or row.get("display_kind") in {"user_message", "process_event"} and not isinstance(row.get("display_events"), list)
        or not message_id or not thread_id
    ):
        return False
    key = (thread_id, message_id)
    if key in seen:
        return True
    publisher = getattr(tui_runtime, "publish_background_response", None)
    if not callable(publisher):
        return False
    if content or row.get("display_events"):
        display_events = row.get("display_events")
        publisher(content, thread_id=thread_id, message_id=message_id, **(
            {"display_events": tuple(display_events)} if isinstance(display_events, list) else {}
        ), **({"gateway_request_id": row["gateway_request_id"]} if isinstance(row.get("gateway_request_id"), str) else {}))
    seen.add(key)
    return True


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
