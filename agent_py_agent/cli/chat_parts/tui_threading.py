# LLM: 本模块只启动 TUI worker 与轻量动画刷新线程；业务事件仍由 worker/runtime 发布，刷新线程不得改 reducer 状态。
# 过程轮询按流身份与序号一起确认；换进程只重基易失游标，不重置 canonical 消息位置。
# 模块用途: 低频刷新后台通知与当前会话只读进程观察；仅同scope已验证帧容忍两次瞬态错误，空新会话不扫owner。

from __future__ import annotations

import errno
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path

from .tui_identity_window import TuiIdentityWindow
from .tui_params import StartWorkerParams, WorkerConfigParams

# TUI 界面刷新最小间隔 0.25 秒，限频重绘
TUI_REFRESH_INTERVAL_SECONDS = 0.25
# S-BG1: 后台主代理轮和子代理面板走 Gateway 轻量快照。会话运行时 用服务端事件推送；
# 当前 HTTP 兼容协议在有任务时按 1 秒刷新，完全空闲时降到 5 秒，兼顾近实时与单 Gateway 负载。
TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS = 1.0
# TUI 完全空闲时后台通知轮询间隔降为 5 秒
TUI_BACKGROUND_NOTICE_IDLE_INTERVAL_SECONDS = 5.0
# 后台通知拉取失败后的首次重试间隔 0.5 秒
TUI_BACKGROUND_NOTICE_FAILURE_INITIAL_SECONDS = 0.5
# 后台通知拉取失败的重试间隔封顶 8 秒
TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS = 8.0
# 去抖：连续失败到这个次数才提示"状态未同步"。单次超时（例如 Gateway 正忙、恰好一次压线）
# 不该在界面上闪一条警告——用户看到的多是噪声，恢复时又要闪一次。取 2 表示"至少两次连续失败"。
TUI_BACKGROUND_NOTICE_WARN_AFTER_FAILURE_COUNT = 2
# 去抖：距上一次成功超过这个秒数才提示。和次数门槛同时满足才显示，避免在快速恢复时误报；
# 3 秒按 1 秒轮询节奏相当于"连续三个周期都没拿到"，足够排除单次抖动。
TUI_BACKGROUND_NOTICE_WARN_AFTER_SECONDS = 3.0
# 同owner/thread已验证帧最多静默保留两次瞬态刷新失败，第三次报告原观察告警。
_BG_WATCH_SILENT_TRANSIENT_FAILURES = 2
_BG_WATCH_TRANSIENT_ERRNOS = frozenset({errno.ENOENT, getattr(errno, "ESTALE", errno.ENOENT), errno.EAGAIN})
_TRANSIENT_BACKGROUND_VIEW_ERRORS = frozenset({"DIRECTORY_CHANGED_DURING_READ", "LOCK_TIMEOUT", "SNAPSHOT_PARSE_INCOMPLETE"})
_HARD_BACKGROUND_VIEW_ERRORS = frozenset({"PERMISSION_DENIED", "OWNER_MISMATCH"})
_BG_WATCH_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})


# LLM: 仅代表本次投影全部错误均已结构化确认瞬态；不携正文、权限或第二份进程快照。
# 类用途: 将可短暂保留旧帧的投影错误与普通OSError区分。
class _BackgroundWatchTransientError(OSError):
    pass


# LLM: 字典和异常按双字段或明确权限类型/errno分类；不看异常文案，不把普通ValueError当硬拒绝。
# 函数用途: 统一列表和真实详情异常的硬拒绝判断，立即撤销旧观察帧。
def _background_watch_hard_projection(result: object) -> bool:
    if isinstance(result, BaseException):
        if isinstance(result, PermissionError):
            return True
        result = {"error_code": getattr(result, "error_code", None), "reason_code": getattr(result, "reason_code", None),
                  "errno": result.errno if isinstance(result, OSError) else None}
    if not isinstance(result, Mapping):
        return False
    errors = result.get("errors")
    entries = (result, *(errors if isinstance(errors, (list, tuple)) else ()))
    return any(isinstance(item, Mapping) and (
        any(isinstance(item.get(field), str) and item[field] in _HARD_BACKGROUND_VIEW_ERRORS for field in ("error_code", "reason_code"))
        or item.get("error") == "PermissionError" or (
            type(item.get("errno")) is int and item["errno"] in _BG_WATCH_PERMISSION_ERRNOS
        )
    ) for item in entries)


# LLM: 全部为准确瞬态code或原有OSError/errno才可宽限；权限、混合、缺失和未知错误不静默，不分析文案。
# 函数用途: 识别目录竞争、锁超时及不完整快照是否可在同scope短暂保留旧帧。
def _background_watch_transient_projection(errors: object) -> bool:
    if not isinstance(errors, (list, tuple)) or not errors:
        return False
    if _background_watch_hard_projection({"errors": errors}):
        return False
    for error in errors:
        if not isinstance(error, Mapping):
            return False
        code = error.get("error_code")
        if isinstance(code, str) and code in _TRANSIENT_BACKGROUND_VIEW_ERRORS:
            continue
        if code or error.get("error") not in {"OSError", "FileNotFoundError", "BlockingIOError"}:
            return False
        number = error.get("errno")
        if type(number) is not int or number not in _BG_WATCH_TRANSIENT_ERRNOS:
            return False
    return True


# LLM: scope变更、未确认和/new仅清易失观察资格，不改进程状态或消费通知。
# 函数用途: 丢弃已验证旧帧资格和连续瞬态计数。
def _reset_background_watch_cache(navigation: object) -> None:
    navigation._background_watch_valid_scope = None
    navigation._bg_watch_consecutive_transient_failures = 0


# LLM: config factory 透传同一 queue/refs/runtime/local_run_ref；不得复制状态或创建第二个控制句柄。
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
        local_run_ref=params.local_run_ref,
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


# LLM: worker 继承原 local_run_ref，刷新只读展示；应用退出沿共享 stop events，不等同停止任务资源。
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
            local_run_ref=params.local_run_ref,
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


# LLM: 去抖状态机：单次超时（Gateway 正忙、恰好压线）不提示，避免警告一闪一灭；只有连续失败
#   达到次数门槛、且距上次成功超过时间门槛才对外报未同步；一旦成功立即清零并刷新成功时刻。
# 类用途: 保存后台通知循环的去抖状态，判定本轮是否应发布同步状态。
class _NoticeWarnDebounce:
    # 函数用途: 建立失败计数与成功时刻基准。
    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.last_success_at = time.monotonic()

    # LLM: 每轮都必须调用（即使没有发布者），否则失败计数与时间基准会漂移。成功恒返回 True，
    #   失败只在两个门槛都满足时返回 True。
    # 函数用途: 记录本轮结果，返回是否应发布本轮同步状态。
    def should_publish(self, snapshot_ok: bool, now: float) -> bool:
        if snapshot_ok:
            self.consecutive_failures = 0
            self.last_success_at = now
            return True
        self.consecutive_failures += 1
        return (
            self.consecutive_failures >= TUI_BACKGROUND_NOTICE_WARN_AFTER_FAILURE_COUNT
            and now - self.last_success_at >= TUI_BACKGROUND_NOTICE_WARN_AFTER_SECONDS
        )


# LLM: 首拍立即读取；前后台任务/进程活动或观察页打开时一秒刷新，空闲五秒；原连接退避与提示去抖不变。
# 函数用途: 按游标同步会话，进程仍在跑或观察页打开时保持一秒节奏，失败只退避不终止任务。
def _background_notice_loop(
    stop_event: threading.Event,
    agent: object,
    session_id: str,
    tui_runtime: object,
    app_ref: list,
    agent_navigation: object | None = None,
    foreground_running_ref: list[bool] | None = None,
) -> None:
    seen = TuiIdentityWindow()
    event_cursor = [0]
    failure_delay = TUI_BACKGROUND_NOTICE_FAILURE_INITIAL_SECONDS
    debounce = _NoticeWarnDebounce()
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
        should_publish = debounce.should_publish(snapshot_ok, time.monotonic())
        health_publisher = getattr(tui_runtime, "publish_background_sync_status", None)
        if should_publish and callable(health_publisher) and health_publisher(ok=snapshot_ok):
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
            navigation_reader = getattr(agent_navigation, "snapshot", None)
            navigation_view = navigation_reader() if callable(navigation_reader) else None
            background_active = background_active or bool(
                getattr(navigation_view, "background_process_count", 0) or getattr(navigation_view, "background_watch_active", False)
            )
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
        if snapshot_ok:
            debounce.last_success_at = time.monotonic()


# LLM: 先消费canonical快照；解析异常显式传给观察同步，不能因finally重读成功变成正常空态，原异常继续退避。
# 函数用途: 同步原通知后刷新同会话进程投影；消费抛错仍清旧观察，不借显示session_id猜线程。
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
    snapshot_ok = False
    scope_error = False
    try:
        if store is None:
            snapshot_ok = _consume_gateway_background_snapshot(
                agent,
                session_id,
                tui_runtime,
                app_ref,
                seen,
                event_cursor,
                agent_navigation,
            )
        else:
            snapshot_ok = _consume_local_background_snapshot(
                agent,
                store,
                session_id,
                tui_runtime,
                app_ref,
                seen,
                event_cursor,
                agent_navigation,
            )
    except Exception:
        scope_error = True
        raise
    finally:
        # 通知消费可能在解析本地绑定时抛错；仍清观察，原异常继续交给外层退避。
        if _sync_background_watch(agent, agent_navigation, session_id=session_id, runtime=tui_runtime, snapshot_ok=snapshot_ok, scope_error=scope_error) and app_ref and app_ref[0] is not None:
            app_ref[0].invalidate()
    return snapshot_ok


# LLM: 真实runtime的锁与reset、快照提交共用；无锁旧显示替身沿原协议兼容，不另造锁，锁内不做IO。
# 函数用途: 一次采样同代的消息游标、过程游标及流身份，避免旧游标配上新代次。
def _capture_background_poll_state(tui_runtime, event_cursor):
    with getattr(tui_runtime, "_lock", nullcontext()):
        return (getattr(tui_runtime, "conversation_reset_generation", 0),
            max(0, int(getattr(tui_runtime, "background_message_cursor", 0))),
            max(0, int(event_cursor[0] or 0)), str(getattr(tui_runtime, "background_event_stream_id", "")))


# LLM: 认证消息页与独立过程流分开消费；起始采样及提交均与reset共锁，消息页请求在锁外。
# 同代消息位置不回退，过程序号仅在换流时重基；类型化活动只影响轮询频率，坏响应沿原退避且保留显示。
# 函数用途: 同步当前会话线程和执行绑定，再按canonical顺序收消息；线程投影只供观察、不改变控制目标。
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
    binding = getattr(tui_runtime, "_background_watch_gateway_binding", None)
    if binding is None or binding[0] != session_id:
        tui_runtime._background_watch_gateway_binding = (session_id, "")
    generation, message_after, event_after, event_stream_id = _capture_background_poll_state(tui_runtime, event_cursor)
    try:
        payload = fetcher(
            session_id,
            after=message_after,
            event_after=event_after,
            event_stream_id=event_stream_id,
        )
    except Exception:
        return False
    # LLM: 原HTTP调用已结束，只在原代次内发表，避免/new后迟到响应重建旧历史。
    # 函数用途: 将本次快照交给带reset代次保护的显示提交。
    def publish():
        return _apply_gateway_background_snapshot(agent, session_id, tui_runtime, app_ref, seen, event_cursor,
            agent_navigation, payload, message_after)
    apply = getattr(tui_runtime, "apply_conversation_snapshot", None)
    if not (apply(generation, publish) if callable(apply) else publish()):
        return False
    view_ok, view_changed = _consume_selected_agent_view(agent, None, session_id, agent_navigation,
        tui_runtime=tui_runtime, expected_generation=generation)
    if view_changed and app_ref[0] is not None:
        app_ref[0].invalidate()
    return view_ok


# LLM: 调用方已完成HTTP读取和reset代次核验；这里只投影原页内存，不请求子页或重建执行/投递身份。
# 函数用途: 同步已确认会话的消息、过程和显示游标。
def _apply_gateway_background_snapshot(agent, session_id, tui_runtime, app_ref, seen, event_cursor, agent_navigation, payload, message_after):
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    target_publisher = getattr(tui_runtime, "publish_execution_target", None)
    if callable(target_publisher):
        target_publisher(payload.get("execution_target"))
    if not _publish_gateway_background_watch_binding(tui_runtime, session_id, payload):
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
    if (
        fresh
        or activity_changed
        or transcript_changed
        or permission_changed
    ) and app_ref[0] is not None:
        app_ref[0].invalidate()
    return True


# LLM: /notices线程只绑定本次session；旧响应只取当轮已校验DTO，不能在切会话取数失败后回退旧DTO。
# 函数用途: 保存观察专用线程投影，区分合法空串与损坏身份；不授予控制权、不触发存储读取。
def _publish_gateway_background_watch_binding(runtime: object, session_id: str, payload: dict) -> bool:
    thread_id = payload.get("thread_id", "")
    target = payload.get("execution_target")
    if "thread_id" not in payload:
        reader = getattr(runtime, "execution_target", None)
        current = reader() if callable(reader) else None
        thread_id = current.get("thread_id", "") if isinstance(current, Mapping) else ""
    valid = isinstance(thread_id, str)
    if valid:
        thread_id = thread_id.strip()
        if "thread_id" in payload and isinstance(target, Mapping) and target.get("thread_id"):
            valid = target["thread_id"] == thread_id
    runtime._background_watch_gateway_binding = (session_id, thread_id if valid else None)
    return valid


# LLM: 本地模式只读同一owner的会话账本与过程流；采样和提交与reset共锁，原始整页取数在锁外。
# 活动计数只控制下次刷新，过程流沿原身份握手且只供显示，不构造第二消息来源。
# 函数用途: 本地模式先读完整页再按reset代次发表；旧页不能恢复旧显示或覆盖新游标，不改变模型输入。
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

    generation, message_after, event_after, event_stream_id = _capture_background_poll_state(tui_runtime, event_cursor)
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
    from ...agent.conversation.agent_activity import conversation_agent_activity

    activity = conversation_agent_activity(agent, store, thread_id).to_dict()
    from ...agent.conversation.background_transcript import (
        read_background_transcript_events,
    )

    transcript = read_background_transcript_events(
        agent,
        thread_id=thread_id,
        after=event_after,
        stream_id=event_stream_id,
    )
    from ...agent.conversation.message_stream import read_background_response_page

    fresh, cursor, messages_ok = read_background_response_page(
        store, thread_id, after=message_after,
        include_display_checkpoints=True,
    )
    if not messages_ok:
        return False
    permission_publisher = _prepare_local_agent_permissions(agent, thread, tui_runtime)

    # LLM: 页及审批IO已在锁外完成；回调只投影内存，旧代不得触碰权限面板、显示、去重和游标。
    # 函数用途: 原子发表当前本地快照，reset前启动的读取整页丢弃。
    def publish():
        permission_changed = permission_publisher() if permission_publisher is not None else False
        activity_changed = _publish_background_activity(tui_runtime, activity, agent_navigation=agent_navigation)
        transcript_ok, transcript_changed = _consume_background_transcript_projection(
            tui_runtime, transcript.get("events"), transcript.get("cursor"), event_cursor,
            stream_id=transcript.get("stream_id"),
        )
        if not transcript_ok:
            return False
        for row in fresh:
            if not _publish_background_notice_row(tui_runtime, row, seen):
                return False
        tui_runtime.background_message_cursor = cursor
        if (
            fresh
            or activity_changed
            or transcript_changed
            or permission_changed
        ) and app_ref[0] is not None:
            app_ref[0].invalidate()
        return True
    guard = getattr(tui_runtime, "apply_conversation_snapshot", None)
    if not (guard(generation, publish) if callable(guard) else publish()):
        return False
    view_ok, view_changed = _consume_selected_agent_view(agent, store, session_id, agent_navigation,
        tui_runtime=tui_runtime, expected_generation=generation)
    if view_changed and app_ref[0] is not None:
        app_ref[0].invalidate()
    return view_ok




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


# LLM: 审批续租/列表读取由调用方在reset锁外执行；仅返回内存面板发表闭包，续租不构成批准。
# 函数用途: 预取本地主/子审批，并把纯显示提交交回原代次围栏。
def _prepare_local_agent_permissions(
    agent: object,
    thread: object,
    tui_runtime: object,
) -> Callable[[], bool] | None:
    syncer = getattr(tui_runtime, "sync_agent_permission_requests", None)
    root_task_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    if not callable(syncer) or not root_task_id:
        return None
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
        return None

    # LLM: 仅用户选择时沿原进程共享事务复核完整请求，预取与面板发表不调用这个回写器。
    # 函数用途: 将嵌入式 TUI 的选择写回同一审批账本。
    def write_decision(run_id, request, decision):
        return resolve_agent_tool_approval(
            agent,
            run_id=run_id,
            request_value=request,
            decision_value=decision,
        )

    # LLM: rows与回写器已在锁外准备；调用方只在原reset代次仍有效时更新内存面板。
    # 函数用途: 发表已读取的审批列表，不在显示提交内续租或读文件。
    def publish():
        return bool(syncer(rows, decision_writer=write_decision))
    return publish


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


# LLM: 根reset锁内采样代次及准确子代理选择，锁外取详情，提交时同锁重验代次和选择；失败保留旧页退避。
# 函数用途: 两种模式共用独立详情围栏，迟到响应不得恢复新会话前的子页或推进其游标。
def _consume_selected_agent_view(
    agent: object,
    store: object | None,
    session_id: str,
    agent_navigation: object | None,
    *,
    tui_runtime: object | None = None,
    expected_generation: int | None = None,
) -> tuple[bool, bool]:
    snapshotter = getattr(agent_navigation, "snapshot", None)
    applier = getattr(agent_navigation, "apply_agent_view", None)
    cursor_reader = getattr(agent_navigation, "event_cursor", None)
    if not callable(snapshotter) or not callable(applier) or not callable(cursor_reader):
        return True, False
    root = tui_runtime if tui_runtime is not None else getattr(agent_navigation, "root_runtime", None)
    with getattr(root, "_lock", nullcontext()):
        generation = getattr(root, "conversation_reset_generation", 0)
        if expected_generation is not None and generation != expected_generation:
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
        payload = None
    changed = False
    # LLM: 只在同代且仍进入同一子代理时消费原DTO；不做IO，旧响应与旧错误都不能污染新页。
    # 函数用途: 原子发表子页及其游标，并返回本次实际显示变化。
    def publish():
        nonlocal changed
        if str(getattr(snapshotter(), "active_run_id", "") or "").strip() != run_id:
            return True
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return False
        changed = bool(applier(run_id, payload))
        return True
    guard = getattr(root, "apply_conversation_snapshot", None)
    ok = guard(generation, publish) if callable(guard) else publish()
    return ok, changed


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
# 函数用途: 仅在有活动时以 4 Hz 驱动动画；实际输入和流式事件独立触发重绘，空闲页不反复刷新。
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


# LLM: 本地读错必须抛出而正常无线程返回None；Gateway只用本session的通知投影或当轮执行DTO，不猜线程。
# 函数用途: 解析只读观察范围；空会话不等于存储损坏，活动claim消失也不等于线程消失。
def _background_watch_thread_id(agent: object, navigation: object | None, *, session_id: str = "", runtime: object | None = None) -> str | None:
    store = getattr(agent, "conversation_store", None)
    resolver = getattr(getattr(store, "threads", None), "resolve_report", None)
    if session_id and callable(resolver):
        from ...agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL

        thread, load_error = resolver(channel=LOCAL_CHAT_CHANNEL, channel_conversation_id=session_id, channel_user_id=LOCAL_AGENT_USER_ID)
        if load_error is not None:
            raise LookupError("background process conversation scope unavailable")
        value = getattr(thread, "thread_id", None)
        return value if isinstance(value, str) and value else None
    root = getattr(navigation, "root_runtime", None) or runtime
    gateway = callable(getattr(agent, "request_background_notices", None))
    if gateway and session_id:
        binding = getattr(root, "_background_watch_gateway_binding", None)
        if binding is None or binding[0] != session_id:
            return None
        if binding[1] is None:
            raise LookupError("background process conversation scope invalid")
        return binding[1] or None
    reader = getattr(root, "execution_target", None)
    target = reader() if callable(reader) else None
    if isinstance(target, Mapping) and isinstance(target.get("thread_id"), str) and target["thread_id"]:
        return target["thread_id"]
    if gateway:
        return None
    for source in (navigation, root, agent):
        value = getattr(source, "thread_id", None)
        if isinstance(value, str) and value:
            return value
    return None


# LLM: 仅绑定owner/thread读列表；权限/owner错误优先显式拒绝，成功完成列表/详情才登记有效帧，未知错误不转瞬态。
# 函数用途: 刷新本会话概要并区分硬拒绝与瞬态竞争，打开观察页才读取选中日志。
def _refresh_background_watch(agent: object, navigation: object | None, *, thread_id: str | None = None) -> bool:
    from ...agent.conversation.background_process_view import (
        inspect_background_process,
        list_background_processes,
    )

    update = getattr(navigation, "update_background_watch_rows", None)
    home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    if not callable(update):
        return False
    if not home:
        if getattr(navigation, "_background_watch_scope", None) is None:
            _reset_background_watch_cache(navigation)
            return False
        raise LookupError("background process owner scope unavailable")
    if thread_id is None:
        thread_id = _background_watch_thread_id(agent, navigation)
    if not thread_id:
        _reset_background_watch_cache(navigation)
        navigation._background_watch_scope = None
        return bool(update([], active_count=0))
    scope = (str(Path(home)), thread_id)
    changed = False
    if getattr(navigation, "_background_watch_scope", None) != scope:
        changed = bool(update([], active_count=0))
        navigation._background_watch_scope = scope
        _reset_background_watch_cache(navigation)
    result = list_background_processes(Path(home), thread_id=thread_id)
    if not result["projection_ok"]:
        if _background_watch_hard_projection(result):
            raise PermissionError("background process permission or owner check failed")
        if _background_watch_transient_projection(result.get("errors")):
            raise _BackgroundWatchTransientError("background process projection unavailable")
        raise OSError("background process projection unavailable")
    rows = sorted(result["items"], key=lambda row: not (row["alive"] or row["wake_pending"]))[:128]
    view = navigation.snapshot()
    if view.background_watch_active:
        selected = view.selected_run_id.removeprefix("bg:")
        for index, row in enumerate(rows):
            if row["session_id"] == selected:
                rows[index] = inspect_background_process(Path(home), selected, thread_id=thread_id)
    updated = bool(update(rows, active_count=result["active_count"]))
    navigation._background_watch_valid_scope = scope
    navigation._bg_watch_consecutive_transient_failures = 0
    return updated or changed


# LLM: 列表和详情异常均识别双字段owner拒绝后清帧/资格/预算；普通错误及同scope瞬态合同不变，不看文案。
# 函数用途: 按canonical线程同步观察，避免列表成功后的详情owner竞态残留旧结果。
def _sync_background_watch(agent: object, navigation: object | None, *, session_id: str = "", runtime: object | None = None, snapshot_ok: bool = True, scope_error: bool = False) -> bool:
    runtime_reader = getattr(navigation, "active_runtime", None)
    if not callable(runtime_reader):
        return False
    display_runtime = runtime_reader()
    scope_confirmed = False
    try:
        if scope_error:
            raise LookupError("background process snapshot parse failed")
        gateway = callable(getattr(agent, "request_background_notices", None))
        resolver = getattr(getattr(getattr(agent, "conversation_store", None), "threads", None), "resolve_report", None)
        thread_id = _background_watch_thread_id(agent, navigation, session_id=session_id, runtime=runtime)
        if not snapshot_ok:
            root = getattr(navigation, "root_runtime", None) or runtime
            binding = getattr(root, "_background_watch_gateway_binding", None)
            known_gateway = gateway and binding is not None and binding[0] == session_id and bool(binding[1])
            if not known_gateway and not navigation.snapshot().background_watch_active and not getattr(navigation, "_background_watch_scope", None):
                return _clear_background_watch(navigation, display_runtime)
            raise LookupError("background process snapshot unavailable")
        if thread_id is None:
            return _clear_background_watch(navigation, display_runtime)
        scope_confirmed = True
        changed = _refresh_background_watch(agent, navigation, thread_id=thread_id)
    except Exception as exc:
        hard_error = _background_watch_hard_projection(exc)
        if hard_error or not scope_confirmed or isinstance(exc, LookupError):
            navigation.update_background_watch_rows([], active_count=0)
            navigation._background_watch_scope = None
            _reset_background_watch_cache(navigation)
            message = ("后台进程观察刷新失败；权限或归属校验失败，已暂停显示" if hard_error else
                       "后台进程观察刷新失败；会话范围未确认，已暂停显示")
        else:
            transient = isinstance(exc, _BackgroundWatchTransientError) or (
                type(exc) in {OSError, FileNotFoundError, BlockingIOError} and exc.errno in _BG_WATCH_TRANSIENT_ERRNOS
            )
            scope = getattr(navigation, "_background_watch_scope", None)
            valid_prior = scope is not None and getattr(navigation, "_background_watch_valid_scope", None) == scope
            if transient and valid_prior:
                count = getattr(navigation, "_bg_watch_consecutive_transient_failures", 0) + 1
                navigation._bg_watch_consecutive_transient_failures = count
                if count <= _BG_WATCH_SILENT_TRANSIENT_FAILURES:
                    return False
            else:
                navigation._bg_watch_consecutive_transient_failures = 0
            message = "后台进程观察刷新失败；仅保留同会话上次结果"
        display_runtime.set_notice(message, duration_seconds=6, notice_kind="background_watch_error")
        return True
    return display_runtime.clear_notice(expected_kind="background_watch_error") or changed


# LLM: 只清观察帧/资格/瞬态计数与同kind提示，不能清掉其它错误或读取全owner进程作为空态回退。
# 函数用途: 统一处理新会话及主页面尚未确认范围时的静默空列表。
def _clear_background_watch(navigation: object, display_runtime: object) -> bool:
    changed = bool(navigation.update_background_watch_rows([], active_count=0))
    navigation._background_watch_scope = None
    _reset_background_watch_cache(navigation)
    return display_runtime.clear_notice(expected_kind="background_watch_error") or changed
