# LLM: 本模块编排 chat TUI 生命周期；TuiRuntime 是显示唯一事实源，worker/按键只通过 typed event 更新它。
# 模块用途: 创建交互界面、后台 worker 和会话级状态，并负责退出收尾。

from __future__ import annotations

import threading
from dataclasses import dataclass

from .rendering import _cprint
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


# LLM: TuiInputRefs 只汇总同一 app 生命周期的共享引用；tui_runtime 不得在 worker 启动后替换。
# 类用途: 把界面、刷新线程、回复历史和 typed TUI runtime 一起交给 worker 启动器。
@dataclass
class TuiInputRefs:
    app_ref: list
    refresh_stop: threading.Event
    assistant_outputs: list[str]
    stop_event: threading.Event
    tui_runtime: object
    agent_navigation: object


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
    pre_run: object | None = None


CONTEXT_WINDOW = 200_000
COLLAPSE_PREVIEW_CHARS = 900


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


def _tui_handle_command(*, params: TuiHandleCommandParams) -> bool:
    from .control_runtime import ChatControlExecution, execute_chat_control
    from .input_loop import handle_common_slash_command, is_exit_command
    from .slash_command_types import SlashCommandContext

    if is_exit_command(params.user):
        _tui_request_exit(_make_tui_exit_refs(params))
        return True
    if params.user == "/expand" or params.user.startswith("/expand "):
        return _tui_handle_expand_command(params.user, params.assistant_outputs)
    return handle_common_slash_command(
        params.user,
        ctx=SlashCommandContext(
            agent=params.agent,
            memory_limit=params.args.memory_limit,
            runtime_inject=params.runtime_inject,
            prompt_files=params.prompt_files,
            print_line=_cprint,
            conversation_id=str(params.current_session_id or "default"),
            control_executor=lambda command: execute_chat_control(
                ChatControlExecution(
                    agent=params.agent,
                    use_gateway=params.use_gateway,
                    state=_tui_control_state(params),
                ),
                command,
            ),
        ),
    )


# LLM: TUI controls consume a lock-protected worker snapshot and never mutate UI refs directly.
# 函数用途：读取 TUI 当前任务、排队数和会话 id。
def _tui_control_state(params: TuiHandleCommandParams) -> ChatControlState:
    from .control_runtime import ChatControlState

    with params.state_lock:
        return ChatControlState(
            running=bool(params.is_running_ref[0]),
            queued_count=int(params.pending_jobs_ref[0]),
            prompt=str(params.running_prompt_ref[0] or ""),
            started_at=float(params.running_started_at_ref[0] or 0.0),
            session_id=str(params.current_session_id or "default"),
            request_id=str(params.running_request_id_ref[0] or ""),
        )


# LLM: app result 只用于 startup readiness 的明确失败码；正常退出/EOF 仍返回 0，finally 始终恢复标题和输出 sink。
# 函数用途: 运行 TUI 事件循环，并执行 worker/preflight 的 pre-run 启动器。
def _run_tui_loop(ctx: TuiLoopContext) -> int:
    from .rendering import set_tui_output_sink

    result = 0
    try:
        with patch_stdout():
            app_result = ctx.app.run(pre_run=ctx.pre_run)
            result = int(app_result or 0)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        title_controller = getattr(ctx.app, "_my_agent_title_controller", None)
        if title_controller is not None:
            title_controller.clear(ctx.app.output)
        set_tui_output_sink(None)
        ctx.refresh_stop.set()
    ctx.stop_event.set()
    _cprint("\nGoodbye.")
    ctx.session_manager.touch_session(ctx.current_session_id, channel="chat")
    return result


# LLM: app 参数必须携带与 worker 相同的 runtime，禁止 UI setup 自行创建第二个 store。
# 函数用途: 从顶层运行配置组装 TUI 界面参数。
def _make_tui_app_params(
    run_config: TuiRunParams,
    assistant_outputs: list[str],
    stop_event: threading.Event,
    tui_runtime: object,
    agent_navigation: object,
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
        runtime_inject=run_config.runtime_inject,
        prompt_files=run_config.prompt_files,
        args=run_config.args,
        use_gateway=run_config.use_gateway,
        paths=run_config.paths,
        assistant_outputs=assistant_outputs,
        shutting_down_ref=run_config.shutting_down_ref,
        running_prompt_ref=run_config.running_prompt_ref,
        running_request_id_ref=run_config.running_request_id_ref,
        stop_event=stop_event,
        current_session_id=run_config.current_session_id,
        tui_runtime=tui_runtime,
        agent_navigation=agent_navigation,
    )


# LLM: worker 参数沿用 refs 中同一 runtime，不能从 session_id 隐式重建事件序号。
# 函数用途: 从顶层配置和界面引用组装后台 worker 参数。
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
        running_request_id_ref=params.running_request_id_ref,
        running_started_at_ref=params.running_started_at_ref,
        agent=params.agent,
        args=params.args,
        paths=params.paths,
        use_gateway=params.use_gateway,
        conversation_history=params.conversation_history,
        history_lock=params.history_lock,
        build_history_context=params.build_history_context,
        assistant_outputs=refs.assistant_outputs,
        last_token_estimate_ref=params.last_token_estimate_ref,
        stop_event=refs.stop_event,
        current_session_id=params.current_session_id,
        tui_runtime=refs.tui_runtime,
        agent_navigation=refs.agent_navigation,
    )


# LLM: run_tui 在任何 app/worker 创建前发布唯一 session_started，并只投影 canonical session 已加载的历史；欢迎卡/恢复正文不经 stdout 建第二事实源。
# 函数用途: 启动一次可恢复历史的完整 prompt_toolkit TUI 会话并等待其退出。
def run_tui(*, params: TuiRunParams) -> int:
    from agent_py_agent import __version__

    from .tui_agent_navigation import TuiAgentNavigationState
    from .tui_runtime import TuiRuntime

    assistant_outputs: list[str] = []
    stop_event = threading.Event()
    app_ref: list = [None]
    refresh_stop = threading.Event()
    tui_runtime = TuiRuntime(params.current_session_id)
    agent_navigation = TuiAgentNavigationState(tui_runtime)
    tui_runtime.publish_session(
        version=__version__,
        model=str(getattr(params.agent.config, "model_name", "") or ""),
        workspace=str(getattr(params.agent, "root", "") or ""),
    )
    tui_runtime.publish_recovered_history(params.conversation_history)

    app = _make_tui_app(
        params=_make_tui_app_params(
            params,
            assistant_outputs,
            stop_event,
            tui_runtime,
            agent_navigation,
        )
    )
    app_ref[0] = app
    refs = TuiInputRefs(
        app_ref=app_ref,
        refresh_stop=refresh_stop,
        assistant_outputs=assistant_outputs,
        stop_event=stop_event,
        tui_runtime=tui_runtime,
        agent_navigation=agent_navigation,
    )
    worker_started = threading.Event()

    # LLM: worker 只能在 direct 模式立即启动，或在真实 Gateway readiness 成功后启动一次；排队输入可以等待但不能抢跑失败连接。
    # 函数用途: 幂等启动当前 TUI 的 worker 与刷新线程。
    def start_workers() -> None:
        if worker_started.is_set() or stop_event.is_set():
            return
        worker_started.set()
        _start_worker_threads(params=_make_start_worker_params(params, refs))

    if params.use_gateway:
        from .tui_preflight import TuiGatewayPreflight, start_tui_gateway_preflight

        # LLM: pre-run 在 prompt_toolkit loop 已建立后才启动探活，避免后台失败先于 Application.future。
        # 函数用途: 为 Gateway TUI 启动一次可见的 readiness 检查。
        def pre_run() -> None:
            start_tui_gateway_preflight(TuiGatewayPreflight(
                application=app,
                runtime=tui_runtime,
                paths=params.paths,
                timeout_seconds=float(
                    getattr(params.agent.config, "gateway_ready_timeout_seconds", 3) or 3
                ),
                stop_event=stop_event,
                on_ready=start_workers,
            ))
    else:
        pre_run = start_workers
    return _run_tui_loop(
        TuiLoopContext(
            app=app,
            refresh_stop=refresh_stop,
            stop_event=stop_event,
            session_manager=params.session_manager,
            current_session_id=params.current_session_id,
            pre_run=pre_run,
        )
    )


def _make_tui_app(*, params: MakeTuiAppParams):
    from .tui_ui_setup import make_tui_app as _make_app

    return _make_app(params=params)


__all__ = [
    "CONTEXT_WINDOW",
    "COLLAPSE_PREVIEW_CHARS",
    "TuiExitRefs",
    "run_tui",
]
