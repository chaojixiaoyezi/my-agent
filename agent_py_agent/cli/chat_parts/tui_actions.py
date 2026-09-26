# LLM: 本模块是 TUI 按键处理器的副作用边界：Gateway 网络请求、后台线程、控制/补充消息持久对账器与
#   本地执行队列投递都在这里；tui_keybindings 只做按键注册与分派并显式导入这些入口。本模块运行时禁止
#   导入 tui_keybindings（避免循环），TuiCreateKeybindingsParams 仅作类型注解在 TYPE_CHECKING 下导入。
#   改动时同步检查 tui_keybindings 的调用点、tui_plugin_commands 的延迟导入和 test_tui_input 等测试的 monkeypatch 路径。
# 模块用途: 集中放置快捷键触发的提交、排队、控制命令、子代理插话/停止与中断等有副作用的动作。

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from .chat_style import CHAT_RESPONSE_STYLE_INJECT
from .input_loop import is_show_prompt_command
from .plain_state import ChatJob
from .tui import _tui_handle_command
from .tui_interaction import TuiDraft
from .tui_params import TuiHandleCommandParams

if TYPE_CHECKING:
    from .tui_keybindings import TuiCreateKeybindingsParams

ACTIVE_TURN_RETRY_INITIAL_SECONDS = 0.25
ACTIVE_TURN_RETRY_MAX_SECONDS = 3.0


# LLM: Gateway memory I/O may traverse or update durable owner state and must never block the
# prompt_toolkit input loop. The worker still calls the one shared slash dispatcher, so TUI and
# plain mode keep identical result wording and memory semantics.
# 函数用途: 在后台执行 /memory 与 /remember，让用户在慢磁盘或慢 Gateway 时仍可继续操作 TUI。
def _tui_submit_gateway_memory_command(
    event: Any,
    params: TuiCreateKeybindingsParams,
    text: str,
) -> bool:
    normalized = str(text or "").strip()
    if normalized != "/memory" and not normalized.startswith(("/memory ", "/remember ")):
        return False
    if not bool(getattr(params, "use_gateway", False)):
        return False
    if getattr(getattr(params, "agent", None), "gateway_client_only", False) is not True:
        return False
    runtime = _required_tui_runtime(params)
    app = event.app
    runtime.set_notice(
        "正在保存记忆…" if normalized.startswith("/remember ") else "正在读取记忆…",
        duration_seconds=2.0,
    )
    command_params = _handle_command_params(params, normalized)

    # LLM: The daemon owns no alternate state or retry loop; it invokes exactly one canonical
    # command and lets the memory transport report success, deterministic failure, or unknown.
    # 函数用途: 执行一次记忆命令并通知界面重绘，不占用输入线程。
    def execute() -> None:
        _tui_handle_command(params=command_params)
        app.invalidate()

    threading.Thread(
        target=execute,
        name="my-agent-tui-memory-command",
        daemon=True,
    ).start()
    return True


# LLM: 只透传与 worker 相同的本地控制引用；快捷键不按界面文本决定资源归属。
# 函数用途: 把输入状态收窄为命令所需的当前消息快照和控制依赖。
def _handle_command_params(params: TuiCreateKeybindingsParams, text: str) -> TuiHandleCommandParams:
    return TuiHandleCommandParams(
        user=text,
        agent=params.agent,
        args=params.args,
        runtime_inject=params.runtime_inject,
        prompt_files=params.prompt_files,
        use_gateway=params.use_gateway,
        paths=params.paths,
        state_lock=params.state_lock,
        is_running_ref=params.is_running_ref,
        pending_jobs_ref=params.pending_jobs_ref,
        running_prompt_ref=params.running_prompt_ref,
        running_request_id_ref=params.running_request_id_ref,
        local_run_ref=params.local_run_ref,
        running_started_at_ref=params.running_started_at_ref,
        shutting_down_ref=params.shutting_down_ref,
        stop_event=params.stop_event,
        assistant_outputs=params.assistant_outputs,
        current_session_id=params.current_session_id,
    )


# LLM: Execution options are snapshotted at Enter time and then travel with either the local job or
# durable active-input outbox. A reconciler/restart must never read mutable CLI lists to rebuild them.
# 函数用途: 冻结当前 TUI 请求的注入、文件、保存、恢复和交互能力选项。
def _tui_execution_options(
    params: TuiCreateKeybindingsParams,
    *,
    include_chat_style: bool = False,
):
    from ...agent.gateway_parts.request_client import GatewayAskExecutionOptions

    inject = tuple(str(item) for item in params.runtime_inject)
    if include_chat_style:
        inject = (*inject, CHAT_RESPONSE_STYLE_INJECT)
    return GatewayAskExecutionOptions(
        inject=inject,
        prompt_files=tuple(str(item) for item in params.prompt_files),
        save=not bool(getattr(params.args, "no_save", False)),
        include_prompt=False,
        resume_context=(
            getattr(params.args, "resume_context", None)
            if isinstance(getattr(params.args, "resume_context", None), bool)
            else None
        ),
        tool_approval=True,
        rich_transcript=True,
    )


# LLM: 用户块与 queue 状态必须先发布到同一 TuiRuntime，再把同 request_id job 放入唯一执行队列。
# 函数用途: 构造含媒体 refs 的聊天任务、显示原草稿并按当前运行快照排队。
def _tui_enqueue_job(
    params: TuiCreateKeybindingsParams,
    text: str,
    *,
    display_text: str | None = None,
    execution_options=None,
    inject_complete: bool = False,
) -> None:
    from ...agent.conversation.control_commands import parse_conversation_task_command
    from ...agent.conversation.models import new_id

    options = execution_options or _tui_execution_options(params)
    visible_text = str(display_text if display_text is not None else text)
    show_prompt, text = is_show_prompt_command(text)
    task_command = parse_conversation_task_command(text)
    system_task: dict[str, object] = {}
    if task_command is not None and task_command.valid:
        text = task_command.prompt
        system_task = task_command.to_request_payload()
    job = ChatJob(
        user=text,
        show_prompt=show_prompt,
        inject=list(options.inject),
        prompt_files=list(options.prompt_files),
        request_id=new_id("chat"),
        system_task=system_task,
        display_text=visible_text,
        save=options.save,
        resume_context=options.resume_context,
        tool_approval=options.tool_approval,
        rich_transcript=options.rich_transcript,
        inject_complete=inject_complete,
        input_media=options.input_media,
    )
    with params.state_lock:
        queued = bool(params.is_running_ref[0]) or int(params.pending_jobs_ref_for_enqueue[0]) > 0
        params.pending_jobs_ref_for_enqueue[0] += 1
    runtime = _required_tui_runtime(params)
    runtime.enqueue_prompt(job.request_id, visible_text, queued=queued)
    params.jobs.put(job)


# LLM: Gateway has already created the canonical queued request. This job only attaches the
# existing request id to the one worker queue; it must never POST the user text a second time.
# 函数用途: 先按确切请求号接回显示、撤下同源副本，再加入本地队列；不二次提交用户文字。
def _tui_attach_gateway_job(
    params: TuiCreateKeybindingsParams,
    text: str,
    *,
    display_text: str,
    gateway_request_id: str,
    client_message_id: str,
    execution_options=None,
) -> None:
    request_id = str(gateway_request_id or "").strip()
    if not request_id:
        raise ValueError("queued active-turn input requires gateway_request_id")
    options = execution_options or _tui_execution_options(params)
    job = ChatJob(
        user=text,
        show_prompt=options.include_prompt,
        inject=list(options.inject),
        prompt_files=list(options.prompt_files),
        request_id=request_id,
        display_text=display_text,
        gateway_request_id=request_id,
        client_message_id=client_message_id,
        save=options.save,
        resume_context=options.resume_context,
        tool_approval=options.tool_approval,
        rich_transcript=options.rich_transcript,
        inject_complete=True,
    )
    with params.state_lock:
        params.pending_jobs_ref_for_enqueue[0] += 1
    runtime = _required_tui_runtime(params)
    runtime.register_gateway_request(request_id)
    runtime.enqueue_prompt(request_id, display_text, queued=True)
    params.jobs.put(job)


# LLM: Mutating Gateway slash controls enter a durable operation outbox before HTTP. `/btw` binds
# an exact live turn; Esc during manual Compact binds that typed control message instead of falling
# through to a foreground/background task. 会话运行时 does not expose manual Compact during an active
# task, so the client must reject that state before persisting an outbox row or starting animation;
# automatic in-turn Compact is a separate runtime path. Ambiguous task sets still fail closed.
# IM 专用的 /admin、/approve、/deny 在写 outbox 之前本地拒绝。
# 函数用途: 展示同源上下文，或持久提交带精确回合/Compact 目标的 TUI 控制命令；任务运行中不显示假的手动压缩动画；单独 /model 留给本地菜单。
def _tui_submit_control_operation(
    params: TuiCreateKeybindingsParams,
    text: str,
) -> bool:
    if not bool(getattr(params, "use_gateway", False)):
        return False
    command, handled = _tui_control_or_im_only_refusal(params, text)
    if handled or command is None or not command.valid:
        return handled
    if command.kind == "model" and command.operation == "view":
        return False  # 单独 /model 仍由 TUI 本地菜单处理；只有带编号的文字形式发给 Gateway。
    if command.kind == "context":
        from .tui_block_renderer import render_tui_context_usage_report

        runtime = _active_tui_runtime(params)
        status = runtime.store.snapshot().status
        runtime.write_console(
            render_tui_context_usage_report(
                status.context_usage,
                compact_count=status.compact_count,
            )
        )
        return True
    runtime = _required_tui_runtime(params)
    if command.kind == "compact":
        with params.state_lock:
            foreground_active = bool(params.is_running_ref[0])
        background_active_reader = getattr(runtime, "has_active_background_task", None)
        background_active = bool(
            callable(background_active_reader) and background_active_reader()
        )
        if foreground_active or background_active:
            runtime.write_console(
                "当前任务仍在运行；请等本轮完成或先使用 /stop，再执行 /compact。"
            )
            return True
    target_control_message_id = ""
    if command.kind == "stop":
        target_control_message_id = runtime.active_manual_compact_control_message_id()
    expected_turn_id = ""
    if command.kind in {"steer", "stop"}:
        with params.state_lock:
            running = bool(params.is_running_ref[0])
            expected_turn_id = str(params.running_request_id_ref[0] or "").strip()
        exact_turn_required = command.kind == "steer" or running
        if exact_turn_required and (not running or not expected_turn_id):
            _required_tui_runtime(params).set_notice(
                "当前没有可精确绑定的运行回合，命令未发送",
                duration_seconds=2.5,
            )
            return True
    from ...agent.conversation.models import new_id
    from .tui_control_delivery import TuiControlOperationEntry

    message_id = new_id("control")
    if command.kind == "steer":
        runtime.enqueue_active_turn_input(message_id, str(command.value or "").strip())
    entry = TuiControlOperationEntry(
        message_id=message_id,
        command_text=str(text or "").strip(),
        command_kind=command.kind,
        expected_turn_id=expected_turn_id,
        target_control_message_id=target_control_message_id,
    )

    # LLM: The control command becomes visible only after the exact outbox row
    # is durable. This callback is presentation-only; it cannot submit a model
    # prompt or become a second command dispatcher.
    # 函数用途: 保存成功后立即在 TUI 历史显示 `/goal`，并在 `/compact` 时同时启动压缩动画。
    def on_control_persisted(persisted) -> None:
        runtime.publish_control_command_input(
            persisted.message_id,
            persisted.command_text,
        )
        if command.kind == "compact":
            runtime.publish_manual_compact_started(persisted.message_id)

    try:
        reconciler = _ensure_control_operation_reconciler(params)
        if command.kind in {"compact", "goal"}:
            reconciler.enqueue(
                entry,
                on_persisted_before_dispatch=on_control_persisted,
            )
        else:
            reconciler.enqueue(entry)
    except Exception:
        if command.kind == "steer":
            runtime.cancel_active_turn_input(message_id)
        runtime.set_notice(
            "控制操作无法保存，命令未发送",
            duration_seconds=2.5,
        )
        return True
    # 会话运行时 的 /status 会立即落一张稳定状态卡；异步刷新不会再把“正在刷新”
    # 留在 footer。这里的状态快照仍由 Gateway 返回，但只读查询不显示成待确认
    # 的副作用操作，避免空闲 TUI 在下一次重绘前一直挂着误导性提示。
    if command.kind != "status":
        runtime.set_notice(
            "补充消息已排队，等待当前回合安全点接收…"
            if command.kind == "steer"
            else "正在确认控制操作…",
            duration_seconds=2.5 if command.kind == "steer" else 1.2,
        )
    return True


# LLM: /admin、/approve、/deny 只属于 IM 私聊：在写控制 outbox、发 Gateway 之前本地拒绝并显示原因，密码不落盘也不离开输入框。
#   返回 (解析结果, 是否已处理)；其余命令原样交回调用方继续走持久控制流程。
# 函数用途: 解析一条 TUI 控制命令，并就地拒绝 IM 专用的管理员命令。
def _tui_control_or_im_only_refusal(params: TuiCreateKeybindingsParams, text: str):
    from ...agent.conversation.control_commands import parse_conversation_control
    from .control_runtime import im_only_control_result

    command = parse_conversation_control(text, reject_unknown_slash=True)
    refused = im_only_control_result(command) if command is not None else None
    if refused is None:
        return command, False
    _required_tui_runtime(params).write_console(refused.message)
    return command, True


# LLM: A single durable reconciler owns every TUI control retry. It reuses the saved message id,
# freezes the exact turn snapshot, and switches permanently to GET after receiving operation_id.
# 函数用途: 创建或取得本会话控制命令对账器，并连接 Gateway 提交、查询与界面回调。
def _ensure_control_operation_reconciler(
    params: TuiCreateKeybindingsParams,
):
    existing = getattr(params, "control_operation_reconciler", None)
    if existing is not None:
        return existing
    from ...agent.conversation.control_commands import parse_conversation_control
    from .control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
        request_gateway_control_status,
    )
    from .tui_control_delivery import (
        TuiControlOperationReconciler,
        tui_control_operation_outbox_path,
    )

    # LLM: Every POST rebuilds only from the persisted row and reuses its exact client message id.
    # 函数用途: 用持久控制行向 Gateway 提交或安全重放同一命令。
    def submit(entry):
        command = parse_conversation_control(entry.command_text, reject_unknown_slash=True)
        if command is None or not command.valid or command.kind != entry.command_kind:
            from ...agent.conversation.control_commands import ConversationControlResult

            return ConversationControlResult(
                entry.command_kind,
                False,
                "持久控制命令已损坏，未发送。",
                control_state="conflict",
            )
        execution = ChatControlExecution(
            agent=params.agent,
            use_gateway=True,
            state=ChatControlState(
                running=bool(entry.expected_turn_id),
                queued_count=0,
                prompt="",
                started_at=0.0,
                session_id=str(params.current_session_id or "default"),
                request_id=entry.expected_turn_id,
            ),
        )
        return execute_chat_control(
            execution,
            command,
            message_id=entry.message_id,
            target_control_message_id=entry.target_control_message_id,
        )

    # LLM: Status requests use only the persisted server operation id and cannot resend command text.
    # 函数用途: 只读查询一条 Gateway 控制操作回执。
    def status(operation_id: str):
        return request_gateway_control_status(
            ChatControlExecution(
                agent=params.agent,
                use_gateway=True,
                state=ChatControlState(
                    running=False,
                    queued_count=0,
                    prompt="",
                    started_at=0.0,
                    session_id=str(params.current_session_id or "default"),
                ),
            ),
            operation_id,
        )

    runtime = _required_tui_runtime(params)

    # LLM: Restart restoration may replay the exact persisted Goal/Compact
    # command as a display block; execution and result authority stay in the
    # same outbox/server receipt and are never inferred from that text.
    # 函数用途: TUI 重启时恢复尚未对账的命令显示，并提示控制操作仍在确认。
    def on_restore(entry) -> None:
        if entry.command_kind == "goal":
            runtime.publish_control_command_input(
                entry.message_id,
                entry.command_text,
            )
        if entry.command_kind == "compact":
            runtime.publish_control_command_input(
                entry.message_id,
                entry.command_text,
            )
            runtime.publish_manual_compact_started(entry.message_id)
        elif entry.command_kind == "steer":
            command = parse_conversation_control(
                entry.command_text,
                reject_unknown_slash=True,
            )
            if command is not None and command.valid:
                try:
                    runtime.enqueue_active_turn_input(
                        entry.message_id,
                        str(command.value or "").strip(),
                    )
                except KeyError:
                    pass
        runtime.set_notice("Restoring control confirmation…", duration_seconds=2.0)

    # LLM: Compact command completion and transcript mutation are separate typed facts. ``ok``
    # closes the command without an error (including an empty-history no-op); only a positive
    # canonical generation may advance the visible compact boundary. Prose never changes state.
    # 函数用途: 展示控制结果；无需压缩时正常收口，真正提交时才刷新历史边界和固定状态栏。
    def on_complete(entry, result) -> None:
        if entry.command_kind == "stop":
            # 后台主回合已经让出时没有 turn_interrupted 事件；只用短 notice 会在
            # 子代理名单刷新前消失，让用户看不出 /stop 是否生效。沿用 会话运行时 的
            # 稳定历史反馈：只投影服务端回执，不据文案猜测任何 run 已到终态。
            runtime.write_console(result.message or "停止请求已完成。")
            return
        if entry.command_kind == "steer":
            if result.delivery_status == "accepted":
                runtime.promote_active_turn_inputs(
                    (entry.message_id,),
                    request_id=entry.expected_turn_id,
                )
                runtime.set_notice("补充消息已进入当前回合", duration_seconds=2.0)
            else:
                runtime.cancel_active_turn_input(entry.message_id)
                runtime.write_console(result.message or "补充消息未进入当前回合。")
            return
        if entry.command_kind == "compact":
            generation = (
                result.status.compact_generation
                if result.ok and result.status is not None
                else None
            )
            runtime.publish_manual_compact_terminal(
                entry.message_id,
                succeeded=result.ok,
                interrupted=result.error_code == "COMPACT_INTERRUPTED",
            )
            if generation is not None and generation > 0:
                runtime.publish_compact_boundary(
                    generation,
                    text=result.message,
                )
                return
        runtime.write_console(result.message or "控制命令已完成。")

    # LLM: Terminal uncertainty is never rendered as success or retried with a fresh id.
    # 函数用途: 告知用户控制副作用无法确认且系统不会自动重复执行。
    def on_terminal_unknown(entry, result) -> None:
        if entry.command_kind == "compact":
            runtime.publish_manual_compact_terminal(
                entry.message_id,
                succeeded=False,
            )
        runtime.set_notice(
            result.message or "Control result is unknown · not repeated",
            duration_seconds=3.0,
        )

    # LLM: Id conflicts are quarantined; the client cannot bypass them by silently making a new id.
    # 函数用途: 提示控制消息身份冲突并停止自动重试。
    def on_conflict(entry) -> None:
        if entry.command_kind == "steer":
            runtime.cancel_active_turn_input(entry.message_id)
        if entry.command_kind == "compact":
            runtime.publish_manual_compact_terminal(
                entry.message_id,
                succeeded=False,
            )
        runtime.set_notice("Control identity conflict · not repeated", duration_seconds=3.0)

    # LLM: Transient outbox errors leave canonical rows untouched and keep the sole worker alive.
    # 函数用途: 提示控制对账暂时失败，后台继续退避。
    def on_error(_error: BaseException) -> None:
        runtime.set_notice("Control confirmation is retrying…", duration_seconds=2.5)

    reconciler = TuiControlOperationReconciler(
        path=tui_control_operation_outbox_path(
            params.paths.root,
            params.current_session_id,
        ),
        submit=submit,
        status=status,
        on_restore=on_restore,
        on_complete=on_complete,
        on_terminal_unknown=on_terminal_unknown,
        on_conflict=on_conflict,
        on_error=on_error,
        stop_event=params.stop_event,
        initial_delay=ACTIVE_TURN_RETRY_INITIAL_SECONDS,
        maximum_delay=ACTIVE_TURN_RETRY_MAX_SECONDS,
    )
    params.control_operation_reconciler = reconciler
    return reconciler


# LLM: 前后台输入共用稳定消息 ID 和持久回执；后台快照仅提供目标提示，服务端重查真实执行权。
# 函数用途: 普通 Enter 进入当前主代理的安全点；只在明确拒绝或已排队时回到原任务队列。
def _tui_submit_active_turn_input(
    params: TuiCreateKeybindingsParams,
    text: str,
    *,
    display_text: str,
) -> bool:
    if not params.use_gateway:
        return False
    from ...agent.conversation.control_commands import parse_conversation_task_command

    show_prompt, _ordinary_text = is_show_prompt_command(text)
    if show_prompt or parse_conversation_task_command(text) is not None:
        return False
    with params.state_lock:
        running = bool(params.is_running_ref[0])
        expected_turn_id = str(params.running_request_id_ref[0] or "").strip()
    runtime = _required_tui_runtime(params)
    if not running:
        expected_turn_id = runtime.background_input_target()
    if not expected_turn_id:
        return False
    if not callable(getattr(params.agent, "request_active_turn_input", None)):
        return False
    from ...agent.conversation.models import new_id

    message_id = new_id("steer")
    runtime.enqueue_active_turn_input(message_id, display_text)
    from .tui_input_delivery import TuiActiveInputOutboxEntry

    execution_options = _tui_execution_options(params, include_chat_style=True)
    reconciler = _ensure_active_input_reconciler(params)
    try:
        reconciler.enqueue(
            TuiActiveInputOutboxEntry(
                message_id=message_id,
                expected_turn_id=expected_turn_id,
                text=text,
                display_text=display_text,
                execution_options=execution_options,
            )
        )
    except Exception:
        runtime.cancel_active_turn_input(message_id)
        runtime.set_notice("Delivery state needs attention · queued next", duration_seconds=2.5)
        return False
    runtime.set_notice("Confirming delivery…", duration_seconds=1.2)
    return True


# LLM: One reconciler and one durable outbox own every uncertain input for this TUI session. The
# instance is created during keymap setup so restart recovery works before the next user message.
# 函数用途: 创建或取得会话级补充消息对账器，并接好 UI 与现有 Gateway job 队列回调。
def _ensure_active_input_reconciler(
    params: TuiCreateKeybindingsParams,
):
    existing = getattr(params, "active_input_reconciler", None)
    if existing is not None:
        return existing
    from ..chat_client_context import ActiveTurnInputDelivery, ActiveTurnInputResult
    from .tui_input_delivery import (
        TuiActiveInputReconciler,
        tui_active_input_outbox_path,
    )

    submit_method = getattr(params.agent, "request_active_turn_input", None)
    status_method = getattr(params.agent, "request_active_turn_input_status", None)

    # LLM: Submission callback always reuses the persisted message and expected-turn ids.
    # 函数用途: 向 Gateway 提交或重放同一条待确认消息。
    def submit(entry):
        if not callable(submit_method):
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        try:
            result = submit_method(
                params.current_session_id,
                message=entry.text,
                message_id=entry.message_id,
                expected_turn_id=entry.expected_turn_id,
                execution_options=entry.execution_options,
            )
        except Exception:
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        return (
            result
            if isinstance(result, ActiveTurnInputResult)
            else ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        )

    # LLM: Once the stable ingress id is known, status polling is read-only and cannot duplicate text.
    # 函数用途: 查询 Gateway 普通消息持久回执。
    def status(request_id: str):
        if not callable(status_method):
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN, request_id=request_id)
        try:
            result = status_method(request_id)
        except Exception:
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN, request_id=request_id)
        return (
            result
            if isinstance(result, ActiveTurnInputResult)
            else ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN, request_id=request_id)
        )

    runtime = _required_tui_runtime(params)

    # LLM: Restart restoration recreates only the local pending block; transport identity remains in
    # the durable outbox.
    # 函数用途: TUI 重启后恢复一条“正在确认”显示。
    def on_restore(entry) -> None:
        try:
            runtime.enqueue_active_turn_input(entry.message_id, entry.display_text)
        except KeyError:
            pass

    # LLM: Durable consumed status repairs a missed stream event using the original exact turn.
    # 函数用途: 将已消费补充消息移入当前回合历史。
    def on_accepted(entry) -> None:
        runtime.promote_active_turn_inputs(
            (entry.message_id,),
            request_id=entry.expected_turn_id,
        )
        runtime.set_notice("Added to current turn", duration_seconds=1.2)

    # LLM: Queued callback attaches the existing stable Gateway request and never POSTs text again.
    # 函数用途: 撤下插入等待项并把服务器已排队请求接到本地 worker。
    def on_queued(entry, request_id: str) -> None:
        if runtime.cancel_active_turn_input(entry.message_id):
            _tui_attach_gateway_job(
                params,
                entry.text,
                display_text=entry.display_text,
                gateway_request_id=request_id,
                client_message_id=entry.message_id,
                execution_options=entry.execution_options,
            )
        runtime.set_notice("Current turn ended · queued next", duration_seconds=1.8)

    # LLM: Explicit rejection proves no Gateway side effect; only then may the ordinary queue create
    # a new request for the same visible text.
    # 函数用途: 明确拒绝时撤下等待项并安全排到下一轮。
    def on_rejected(entry) -> None:
        if runtime.cancel_active_turn_input(entry.message_id):
            _tui_enqueue_job(
                params,
                entry.text,
                display_text=entry.display_text,
                execution_options=entry.execution_options,
                inject_complete=True,
            )
        runtime.set_notice("Current turn ended · queued next", duration_seconds=1.8)

    # LLM: Reusing one immutable client id with another payload is quarantined. Retrying or queueing
    # the conflicting text would bypass the idempotency authority and create a second side effect.
    # 函数用途: 撤下冲突消息的等待显示并提示用户重新输入，不自动创建新任务。
    def on_conflict(entry) -> None:
        runtime.cancel_active_turn_input(entry.message_id)
        runtime.set_notice("Message identity conflict · please send again", duration_seconds=3.0)

    # LLM: Disk/corruption errors must not kill the sole reconciler silently; the durable row stays
    # untouched while the user receives a non-authoritative diagnostic notice.
    # 函数用途: 提示补充消息对账暂时失败，后台继续按退避重试。
    def on_error(_error: BaseException) -> None:
        runtime.set_notice("Delivery confirmation is retrying…", duration_seconds=2.5)

    reconciler = TuiActiveInputReconciler(
        path=tui_active_input_outbox_path(params.paths.root, params.current_session_id),
        submit=submit,
        status=status,
        on_restore=on_restore,
        on_accepted=on_accepted,
        on_queued=on_queued,
        on_rejected=on_rejected,
        on_conflict=on_conflict,
        on_error=on_error,
        stop_event=params.stop_event,
        initial_delay=ACTIVE_TURN_RETRY_INITIAL_SECONDS,
        maximum_delay=ACTIVE_TURN_RETRY_MAX_SECONDS,
    )
    params.active_input_reconciler = reconciler
    return reconciler


# LLM: runtime 缺失是主链接线错误，不能退回 `_cprint` 建立第二 transcript。
# 函数用途: 校验并取得 typed TUI runtime。
def _required_tui_runtime(params: TuiCreateKeybindingsParams):
    from .tui_runtime import TuiRuntime

    if not isinstance(params.tui_runtime, TuiRuntime):
        raise TypeError("TuiCreateKeybindingsParams.tui_runtime must be TuiRuntime")
    return params.tui_runtime


# LLM: Agent navigation may select a child display runtime, but the root runtime
# remains the only worker/queue runtime. Callers use this helper for visible notices
# and child guidance receipts only.
# 函数用途: 返回当前代理页面对应的 typed TUI runtime。
def _active_tui_runtime(params: TuiCreateKeybindingsParams):
    reader = getattr(getattr(params, "agent_navigation", None), "active_runtime", None)
    runtime = reader() if callable(reader) else _required_tui_runtime(params)
    from .tui_runtime import TuiRuntime

    if not isinstance(runtime, TuiRuntime):
        raise TypeError("active agent runtime must be TuiRuntime")
    return runtime


# LLM: A child input gets one stable client id and an idempotent Gateway mailbox
# submission. HTTP acceptance leaves the pending row visible; only the child
# provider-consumed event may promote it into transcript history. Network
# uncertainty retries only that same id and never creates a main-agent ChatJob.
# 函数用途: 将普通自然语言排进当前子代理，并一直显示到模型真正收到为止。
def _tui_submit_agent_input(
    event,
    params: TuiCreateKeybindingsParams,
    *,
    run_id: str,
    text: str,
    display_text: str,
) -> None:
    from ...agent.conversation.models import new_id

    message_id = new_id("agent-steer")
    runtime = _active_tui_runtime(params)
    runtime.enqueue_active_turn_input(message_id, display_text)
    sender = getattr(params.agent, "request_agent_guidance", None)
    if not callable(sender):
        runtime.cancel_active_turn_input(message_id)
        runtime.set_notice("当前连接不支持子代理插话", duration_seconds=2.5)
        _restore_failed_agent_input(event.app, params, text)
        return
    app = event.app

    # LLM: The retry loop is bounded by the TUI stop event and reuses the exact
    # idempotency identity. Only a structured success removes the pending receipt.
    # 函数用途: 在后台确认补充消息已进入子代理消息箱，或把明确失败的正文恢复到输入框。
    def deliver() -> None:
        delay = ACTIVE_TURN_RETRY_INITIAL_SECONDS
        while not params.stop_event.is_set():
            try:
                result = sender(
                    params.current_session_id,
                    run_id=run_id,
                    message=text,
                    message_id=message_id,
                )
            except Exception:
                result = {"ok": False, "http_status": 0}
            if isinstance(result, dict) and result.get("ok") is True:
                # Gateway 回执只证明消息箱已接受；保持 pending，等待 child
                # provider-consumed 事件按同一 message_id 原子提升为用户消息。
                runtime.set_notice(
                    _agent_guidance_sent_notice(display_text),
                    duration_seconds=2.5,
                )
                app.invalidate()
                return
            status = _safe_http_status(result)
            if status in {0, 500, 502, 503, 504}:
                runtime.set_notice("正在确认子代理消息…", duration_seconds=2.0)
                if params.stop_event.wait(delay):
                    return
                delay = min(ACTIVE_TURN_RETRY_MAX_SECONDS, delay * 2.0)
                continue
            runtime.cancel_active_turn_input(message_id)
            code = str(result.get("error_code") or "") if isinstance(result, dict) else ""
            runtime.set_notice(
                "子代理已结束，消息未发送"
                if code == "AGENT_ALREADY_TERMINAL"
                else "消息未发送，已恢复到输入框",
                duration_seconds=2.8,
            )
            _restore_failed_agent_input(app, params, text)
            return

    # 先发布“正在确认”再启动 worker，避免极快回执先写入
    # “已排队”后又被主线程的旧状态覆盖。
    runtime.set_notice("正在确认子代理消息…", duration_seconds=1.2)
    threading.Thread(target=deliver, daemon=True).start()
    app.invalidate()


# LLM: The acknowledgement is a bounded preview of one mailbox-accepted but not
# yet provider-consumed child input. It carries no delivery or reply authority.
# 函数用途: 提示消息已进入子代理排队区，稍后由结构化消费事件收口。
def _agent_guidance_sent_notice(text: str) -> str:
    preview = " ".join(str(text or "").split())
    if len(preview) > 32:
        preview = preview[:31].rstrip() + "…"
    return f"已排队给当前子代理：{preview}" if preview else "已排队给当前子代理"


# LLM: Failed delivery restoration runs on the prompt_toolkit event loop. It
# never overwrites text the user typed while the network request was pending.
# `_set_input_draft` 是按键层共享的输入框投影，留在 tui_keybindings；这里在事件循环回调内
# 延迟导入，避免 tui_actions 在模块加载时反向导入 tui_keybindings 形成循环。
# 函数用途: 明确未投递时把原消息放回空输入框，非空时留在正文中供用户复制。
def _restore_failed_agent_input(
    app: object,
    params: TuiCreateKeybindingsParams,
    text: str,
) -> None:
    def restore() -> None:
        from .tui_keybindings import _set_input_draft

        if not str(params.input_area.text or ""):
            _set_input_draft(params, TuiDraft(text, len(text)))
        else:
            _active_tui_runtime(params).write_console(
                "上一条给子代理的消息未发送，请重新提交。"
            )
        app.invalidate()

    loop = getattr(app, "loop", None)
    schedule = getattr(loop, "call_soon_threadsafe", None)
    if callable(schedule):
        schedule(restore)
    else:
        restore()


# LLM: Esc in a child view submits one exact run cancellation on a background
# thread. It never calls the root `/stop` path or uses the row index as identity.
# 函数用途: 停止当前正在查看的子代理，并立即给用户可见反馈。
def _dispatch_agent_interrupt(
    event,
    params: TuiCreateKeybindingsParams,
    run_id: str,
) -> None:
    from ...agent.conversation.models import new_id

    runtime = _active_tui_runtime(params)
    requester = getattr(params.agent, "request_agent_stop", None)
    if not callable(requester):
        runtime.set_notice("当前连接不支持停止子代理", duration_seconds=2.5)
        event.app.invalidate()
        return
    operation_id = new_id("agent-stop")
    runtime.set_notice("正在停止当前子代理…", duration_seconds=2.0)
    app = event.app

    # LLM: The server's canonical cancellation result is the only completion
    # signal; client-side notice text never changes run status.
    # 函数用途: 在后台发送停止请求并显示确认结果。
    def stop_agent() -> None:
        try:
            result = requester(
                params.current_session_id,
                run_id=run_id,
                operation_id=operation_id,
            )
        except Exception:
            result = {"ok": False}
        if isinstance(result, dict) and result.get("ok") is True:
            status = str(result.get("status") or "").strip()
            message = (
                "这个子代理已经停止"
                if status == "already_terminal"
                else "停止请求已接收，正在收口…"
            )
            runtime.set_notice(message, duration_seconds=2.4)
        else:
            runtime.set_notice(
                "停止结果暂时无法确认；系统没有自动重复执行",
                duration_seconds=3.0,
            )
        app.invalidate()

    threading.Thread(target=stop_agent, daemon=True).start()
    event.app.invalidate()


# LLM: HTTP status is transport metadata only and cannot decide child lifecycle.
# 函数用途: 安全读取薄客户端返回的 HTTP 状态码。
def _safe_http_status(value: object) -> int:
    try:
        return int(value.get("http_status") or 0) if isinstance(value, dict) else 0
    except (TypeError, ValueError):
        return 0


# LLM: Esc 只中断本轮；网络不占 UI 线程，Goal 的续接交由服务端原执行权与去重 wake。
# 函数用途: 立即显示中断反馈并派发 /interrupt；显式 /stop 仍可暂停目标。
def _dispatch_active_interrupt(event, params: TuiCreateKeybindingsParams) -> None:
    if not params.use_gateway:
        _required_tui_runtime(params).request_interrupt()
        event.app.invalidate()
        _tui_handle_command(params=_handle_command_params(params, "/interrupt"))
        return
    with params.state_lock:
        exact_turn_id = str(params.running_request_id_ref[0] or "").strip()
    if not exact_turn_id:
        _required_tui_runtime(params).set_notice(
            "Turn is still binding · stop was not sent",
            duration_seconds=2.5,
        )
        event.app.invalidate()
        return
    _required_tui_runtime(params).request_interrupt()
    _tui_submit_control_operation(params, "/interrupt")
    event.app.invalidate()
