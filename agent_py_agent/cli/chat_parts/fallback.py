# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

import time

from .fallback_refs import FallbackEnqueueParams, FallbackInputRefs, WorkerStateRefs
from .fallback_state import (
    FALLBACK_CHAT_PROMPT,
    MAX_HISTORY_TURNS,
    ChatJob,
    FallbackHandleCommandConfig,
    RunFallbackConfig,
    _startup_banner,
    append_conversation_turn,
    render_gateway_status,
)
from .fallback_ui import _handle_expand_command, _read_user_input
from .fallback_worker import _start_fallback_worker
from .input_loop import handle_common_slash_command, is_exit_command, is_show_prompt_command
from .rendering import BLUE, BOLD, RESET, terminal_rule
from .slash_command_types import SlashCommandContext


# LLM: _show_status 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _show_status(
    state: WorkerStateRefs,
    agent,
    paths,
    use_gateway: bool,
) -> None:
    with state.state_lock:
        active = state.pending_jobs_ref[0] + (1 if state.is_running_ref[0] else 0)
        prompt = state.running_prompt_ref[0]
        elapsed = (
            time.perf_counter() - state.running_started_at_ref[0]
            if state.is_running_ref[0]
            else 0
        )
    _print_worker_status(state, active, prompt, elapsed)
    if use_gateway:
        for line in render_gateway_status(agent, paths):
            print(line)


# LLM: _print_worker_status 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_worker_status(
    state: WorkerStateRefs, active: int, prompt: str, elapsed: float
) -> None:
    if not active:
        print("当前没有后台任务。")
        return
    if state.is_running_ref[0]:
        print(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {state.pending_jobs_ref[0]} 个任务。")
        print(f"当前任务: {prompt}")
        return
    print(f"当前没有运行中的任务；队列中还有 {state.pending_jobs_ref[0]} 个任务。")


# LLM: _fallback_handle_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _fallback_handle_command(cfg: FallbackHandleCommandConfig) -> bool | None:
    if is_exit_command(cfg.user):
        _wait_for_exit(cfg)
        return True
    if cfg.user == "/expand" or cfg.user.startswith("/expand "):
        _handle_expand_command(cfg.user, cfg.assistant_outputs)
        return False
    if cfg.user == "/status":
        _show_status(_worker_state_refs(cfg), cfg.agent, cfg.paths, cfg.use_gateway)
        return False
    if _handle_shared_slash_command(cfg):
        return False
    return None


# LLM: _wait_for_exit 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _wait_for_exit(cfg: FallbackHandleCommandConfig) -> None:
    with cfg.state_lock:
        active = cfg.pending_jobs_ref[0] + (1 if cfg.is_running_ref[0] else 0)
    if active:
        print(f"还有 {active} 个后台任务，等待完成后退出。按 Ctrl+C 可强制退出。")
        cfg.jobs.join()
    print("再见。")


# LLM: _worker_state_refs 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _worker_state_refs(cfg: FallbackHandleCommandConfig) -> WorkerStateRefs:
    return WorkerStateRefs(
        state_lock=cfg.state_lock,
        is_running_ref=cfg.is_running_ref,
        pending_jobs_ref=cfg.pending_jobs_ref,
        running_prompt_ref=cfg.running_prompt_ref,
        running_started_at_ref=cfg.running_started_at_ref,
    )


# LLM: _handle_shared_slash_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_shared_slash_command(cfg: FallbackHandleCommandConfig) -> bool:
    return handle_common_slash_command(
        cfg.user,
        ctx=SlashCommandContext(
            agent=cfg.agent,
            memory_limit=cfg.args.memory_limit,
            runtime_inject=cfg.runtime_inject,
            prompt_files=cfg.prompt_files,
            print_line=print,
        ),
        include_fallback_help=True,
    )


# LLM: _fallback_enqueue_job 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _fallback_enqueue_job(params: FallbackEnqueueParams) -> ChatJob:
    show_prompt, text = is_show_prompt_command(params.user)
    job = ChatJob(
        user=text,
        show_prompt=show_prompt,
        inject=list(params.runtime_inject_list),
        prompt_files=list(params.prompt_files),
    )
    with params.state_lock:
        params.pending_jobs_ref[0] += 1
    params.jobs.put(job)
    return job


# LLM: _print_fallback_banner 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_fallback_banner(cfg: RunFallbackConfig) -> None:
    print(_startup_banner(cfg.agent.config.agent_name, use_gateway=cfg.use_gateway))
    print(f"{cfg.agent.config.agent_name} 交互循环已启动 [v2 fallback模式]。")
    print("输入 /help 查看命令，输入 /exit 退出。")
    if cfg.use_gateway:
        print("当前模式: gateway 客户端。普通消息会投递给后台 gateway 处理。")


# LLM: _fallback_handle_user_input 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _fallback_handle_user_input(
    cfg: RunFallbackConfig,
    user: str,
    refs: FallbackInputRefs,
) -> bool | None:
    exit_result = _fallback_handle_command(_fallback_command_config(cfg, user, refs))
    if exit_result is not None:
        return exit_result
    return None


# LLM: _fallback_command_config 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _fallback_command_config(
    cfg: RunFallbackConfig,
    user: str,
    refs: FallbackInputRefs,
) -> FallbackHandleCommandConfig:
    return FallbackHandleCommandConfig(
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
        running_started_at_ref=refs.running_started_at_ref,
        paths=cfg.paths,
        assistant_outputs=refs.assistant_outputs,
        jobs=cfg.jobs,
    )


# LLM: _make_fallback_refs 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _make_fallback_refs() -> FallbackInputRefs:
    return FallbackInputRefs(
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_prompt_ref=[""],
        running_started_at_ref=[0.0],
        assistant_outputs=[],
    )


# LLM: _read_fallback_user 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 读取文件、索引或配置，并转换成后续逻辑可直接使用的数据。
def _read_fallback_user(cfg: RunFallbackConfig, waiting_ref: list[bool]) -> str | None:
    try:
        return _read_user_input(cfg.state_lock, waiting_ref)
    except (EOFError, KeyboardInterrupt):
        print("\n再见。")
        return None


# LLM: _handle_fallback_message 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
def _handle_fallback_message(
    cfg: RunFallbackConfig,
    user: str,
    refs: FallbackInputRefs,
) -> bool:
    exit_result = _fallback_handle_user_input(cfg, user, refs)
    if exit_result is not None:
        return exit_result
    _fallback_enqueue_job(_enqueue_params(cfg, user, refs))
    _render_user_entry(user)
    return False


# LLM: _enqueue_params 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _enqueue_params(
    cfg: RunFallbackConfig, user: str, refs: FallbackInputRefs
) -> FallbackEnqueueParams:
    return FallbackEnqueueParams(
        user=user,
        jobs=cfg.jobs,
        state_lock=cfg.state_lock,
        pending_jobs_ref=refs.pending_jobs_ref,
        runtime_inject_list=cfg.runtime_inject,
        prompt_files=cfg.prompt_files,
    )


# LLM: _render_user_entry 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _render_user_entry(user: str) -> None:
    print(f"\n{terminal_rule()}")
    print(f"{BLUE}●{RESET}  {BLUE}{BOLD}{user}{RESET}")


# LLM: _run_fallback_input_loop 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def _run_fallback_input_loop(cfg: RunFallbackConfig, refs: FallbackInputRefs) -> None:
    waiting_ref = [False]
    while True:
        user = _read_fallback_user(cfg, waiting_ref)
        if user is None:
            break
        if user and _handle_fallback_message(cfg, user, refs):
            break


# LLM: run_fallback 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_fallback(cfg: RunFallbackConfig) -> int:
    refs = _make_fallback_refs()
    _start_fallback_worker(cfg, refs)
    _print_fallback_banner(cfg)
    _run_fallback_input_loop(cfg, refs)
    cfg.session_manager.touch_session(cfg.current_session_id, channel="chat")
    return 0


__all__ = [
    "FALLBACK_CHAT_PROMPT",
    "MAX_HISTORY_TURNS",
    "ChatJob",
    "append_conversation_turn",
    "run_fallback",
]
