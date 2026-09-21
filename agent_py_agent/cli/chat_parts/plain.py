# LLM: 普通终端只分派输入和控制快照；运行绑定从原 worker 句柄取得，不根据界面文本选择资源。
# 模块用途: 驱动普通聊天输入、共享命令与队列。
from __future__ import annotations

from ...agent.conversation.control_commands import parse_conversation_task_command
from ...agent.conversation.models import new_id
from .control_runtime import ChatControlExecution, ChatControlState, execute_chat_control
from .input_loop import handle_common_slash_command, is_exit_command, is_show_prompt_command
from .plain_state import (
    MAX_HISTORY_TURNS,
    PLAIN_CHAT_PROMPT,
    ChatJob,
    PlainEnqueueParams,
    PlainHandleCommandConfig,
    PlainInputRefs,
    RunPlainConfig,
    append_conversation_turn,
)
from .plain_ui import _handle_expand_command, _read_user_input
from .plain_worker import _start_plain_worker
from .rendering import BLUE, BOLD, RESET, startup_banner, terminal_rule
from .slash_command_types import SlashCommandContext


def _plain_handle_command(cfg: PlainHandleCommandConfig) -> bool | None:
    if is_exit_command(cfg.user):
        _wait_for_exit(cfg)
        return True
    if cfg.user == "/expand" or cfg.user.startswith("/expand "):
        _handle_expand_command(cfg.user, cfg.assistant_outputs)
        return False
    if _handle_shared_slash_command(cfg):
        return False
    return None


def _wait_for_exit(cfg: PlainHandleCommandConfig) -> None:
    with cfg.state_lock:
        active = cfg.pending_jobs_ref[0] + (1 if cfg.is_running_ref[0] else 0)
    if active:
        print(f"还有 {active} 个后台任务，等待完成后退出。按 Ctrl+C 可强制退出。")
        cfg.jobs.join()
    print("再见。")


def _handle_shared_slash_command(cfg: PlainHandleCommandConfig) -> bool:
    return handle_common_slash_command(
        cfg.user,
        ctx=SlashCommandContext(
            agent=cfg.agent,
            memory_limit=cfg.args.memory_limit,
            runtime_inject=cfg.runtime_inject,
            prompt_files=cfg.prompt_files,
            print_line=print,
            conversation_id=str(cfg.current_session_id or "default"),
            control_executor=lambda command: execute_chat_control(
                ChatControlExecution(
                    agent=cfg.agent,
                    use_gateway=cfg.use_gateway,
                    state=_plain_control_state(cfg),
                ),
                command,
            ),
        ),
        include_plain_help=True,
    )


def _plain_enqueue_job(params: PlainEnqueueParams) -> ChatJob:
    show_prompt, text = is_show_prompt_command(params.user)
    task_command = parse_conversation_task_command(text)
    system_task: dict[str, object] = {}
    if task_command is not None and task_command.valid:
        text = task_command.prompt
        system_task = task_command.to_request_payload()
    job = ChatJob(
        user=text,
        show_prompt=show_prompt,
        inject=list(params.runtime_inject_list),
        prompt_files=list(params.prompt_files),
        request_id=new_id("chat"),
        system_task=system_task,
    )
    with params.state_lock:
        params.pending_jobs_ref[0] += 1
    params.jobs.put(job)
    return job


# LLM: 状态锁只读取 worker 快照和原调用句柄；执行身份在句柄内发布，不从界面消息编号推导。
# 函数用途: 把普通终端当前任务与本地控制句柄一起交给命令端。
def _plain_control_state(cfg: PlainHandleCommandConfig) -> ChatControlState:
    with cfg.state_lock:
        return ChatControlState(
            running=bool(cfg.is_running_ref[0]),
            queued_count=int(cfg.pending_jobs_ref[0]),
            prompt=str(cfg.running_prompt_ref[0] or ""),
            started_at=float(cfg.running_started_at_ref[0] or 0.0),
            session_id=str(cfg.current_session_id or "default"),
            request_id=str(cfg.running_request_id_ref[0] or ""),
            local_run=cfg.local_run_ref[0],
        )


def _print_plain_banner(cfg: RunPlainConfig) -> None:
    print(startup_banner(cfg.agent.config.agent_name, use_gateway=cfg.use_gateway))
    print(f"{cfg.agent.config.agent_name} 交互循环已启动 [普通终端模式]。")
    print("输入 /help 查看命令，输入 /exit 退出。")
    if cfg.use_gateway:
        print("当前模式: gateway 客户端。普通消息会投递给后台 gateway 处理。")


def _plain_handle_user_input(
    cfg: RunPlainConfig,
    user: str,
    refs: PlainInputRefs,
) -> bool | None:
    exit_result = _plain_handle_command(_plain_command_config(cfg, user, refs))
    if exit_result is not None:
        return exit_result
    return None


# LLM: 透传与 worker 相同的运行引用，特别是 local_run_ref；不能另造控制状态。
# 函数用途: 为当前普通终端输入组装命令执行依赖。
def _plain_command_config(
    cfg: RunPlainConfig,
    user: str,
    refs: PlainInputRefs,
) -> PlainHandleCommandConfig:
    return PlainHandleCommandConfig(
        user=user,
        agent=cfg.agent,
        args=cfg.args,
        runtime_inject=cfg.runtime_inject,
        prompt_files=cfg.prompt_files,
        use_gateway=cfg.use_gateway,
        state_lock=cfg.state_lock,
        is_running_ref=refs.is_running_ref,
        pending_jobs_ref=refs.pending_jobs_ref,
        running_prompt_ref=refs.running_prompt_ref,
        running_request_id_ref=refs.running_request_id_ref,
        local_run_ref=refs.local_run_ref,
        running_started_at_ref=refs.running_started_at_ref,
        paths=cfg.paths,
        assistant_outputs=refs.assistant_outputs,
        jobs=cfg.jobs,
        current_session_id=cfg.current_session_id,
    )


def _make_plain_input_refs() -> PlainInputRefs:
    return PlainInputRefs(
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_prompt_ref=[""],
        running_request_id_ref=[""],
        running_started_at_ref=[0.0],
        assistant_outputs=[],
    )


def _read_plain_user(cfg: RunPlainConfig, waiting_ref: list[bool]) -> str | None:
    try:
        return _read_user_input(cfg.state_lock, waiting_ref)
    except (EOFError, KeyboardInterrupt):
        return None


def _handle_plain_message(
    cfg: RunPlainConfig,
    user: str,
    refs: PlainInputRefs,
) -> bool:
    exit_result = _plain_handle_user_input(cfg, user, refs)
    if exit_result is not None:
        return exit_result
    _plain_enqueue_job(_enqueue_params(cfg, user, refs))
    _render_user_entry(user)
    return False


def _enqueue_params(
    cfg: RunPlainConfig, user: str, refs: PlainInputRefs
) -> PlainEnqueueParams:
    return PlainEnqueueParams(
        user=user,
        jobs=cfg.jobs,
        state_lock=cfg.state_lock,
        pending_jobs_ref=refs.pending_jobs_ref,
        runtime_inject_list=cfg.runtime_inject,
        prompt_files=cfg.prompt_files,
    )


def _render_user_entry(user: str) -> None:
    print(f"\n{terminal_rule()}")
    print(f"{BLUE}●{RESET}  {BLUE}{BOLD}{user}{RESET}")


def _run_plain_input_loop(cfg: RunPlainConfig, refs: PlainInputRefs) -> bool:
    """Read input until EOF or an explicit command completes shutdown.

    A false return means input ended without waiting for already queued work.
    The caller must then run the same orderly shutdown used by ``/exit``.
    """

    waiting_ref = [False]
    while True:
        user = _read_plain_user(cfg, waiting_ref)
        if user is None:
            return False
        if user and _handle_plain_message(cfg, user, refs):
            return True


def run_plain(cfg: RunPlainConfig) -> int:
    refs = _make_plain_input_refs()
    _start_plain_worker(cfg, refs)
    _print_plain_banner(cfg)
    shutdown_complete = _run_plain_input_loop(cfg, refs)
    if not shutdown_complete:
        _wait_for_exit(_plain_command_config(cfg, "/exit", refs))
    cfg.session_manager.touch_session(cfg.current_session_id, channel="chat")
    return 0


__all__ = [
    "PLAIN_CHAT_PROMPT",
    "MAX_HISTORY_TURNS",
    "ChatJob",
    "append_conversation_turn",
    "run_plain",
]
