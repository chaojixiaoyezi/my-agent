"""LLM: fallback worker for non-streaming responses in chat mode.

给人看的解释：
当终端不支持 prompt_toolkit 时，用纯 stdlib input() 的回退循环。
保持和 TUI 模式相同的功能，只是实现更简单。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .gateway_client import (
    check_gateway_alive,
    format_gateway_timing,
    poll_gateway_chunks,
    submit_chat_request,
)
from .input_loop import (
    handle_common_slash_command,
    is_exit_command,
    is_show_prompt_command,
    parse_expand_target,
)
from .rendering import (
    BLUE,
    BOLD,
    GRAY,
    GREEN,
    collapse_response_text,
    terminal_rule,
)


@dataclass
class FallbackWorkerConfig:
    """Bundle of all _fallback_worker parameters."""

    jobs: Any  # queue.Queue
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    agent: Any
    args: Any
    paths: Any
    use_gateway: bool
    assistant_outputs: list[str]
    conversation_history: list[tuple[str, str]]
    history_lock: threading.Lock
    build_history_context: Callable[[], str]


@dataclass
class FallbackHandleCommandConfig:
    """Bundle of all _fallback_handle_command parameters."""

    user: str
    agent: Any
    args: Any
    runtime_inject: list[str]
    prompt_files: list[str]
    use_gateway: bool
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    paths: Any
    assistant_outputs: list[str]
    jobs: Any  # queue.Queue


@dataclass
class RunFallbackConfig:
    """Bundle of all run_fallback parameters."""

    agent: Any
    args: Any
    use_gateway: bool
    paths: Any
    runtime_inject: list[str]
    prompt_files: list[str]
    conversation_history: list[tuple[str, str]]
    history_lock: threading.Lock
    jobs: Any  # queue.Queue
    state_lock: threading.Lock
    build_history_context: Callable[[], str]
    session_manager: Any
    current_session_id: str


# Constants and helpers needed by fallback (imported from chat.py context)
_CHAT_RESPONSE_STYLE_INJECT = (
    "这是 CLI 聊天界面。回答风格要求："
    "1. 不要用模板化欢迎词；"
    "2. 不要在结尾主动列出'你可以问我这三个问题'这类建议问题；"
    "3. 直接围绕用户当前输入回答，除非用户要求，否则不要做教学式铺垫；"
    "4. 除非用户明确要求，不要先介绍你会什么、不要先列能力清单；"
    "5. 默认优先用简短自然语言回答，不要动不动列 1、2、3。"
)

FALLBACK_CHAT_PROMPT = "❯ "
MAX_HISTORY_TURNS = 8


def _startup_banner(agent_name: str, *, use_gateway: bool) -> str:
    """Build startup banner."""
    from .rendering import startup_banner as _sb

    return _sb(agent_name, use_gateway=use_gateway)


def append_conversation_turn(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    user_message: str,
    assistant_message: str,
    *,
    max_turns: int = MAX_HISTORY_TURNS,
) -> None:
    """Append one turn and keep the in-memory buffer bounded."""
    with history_lock:
        conversation_history.append((user_message, assistant_message))
        if len(conversation_history) > max_turns * 2:
            conversation_history[:] = conversation_history[-max_turns:]


def render_gateway_status(agent, paths):
    """Render gateway status lines."""
    from ...agent.gateway import render_gateway_status as _rgs

    return _rgs(agent, paths)


def resume_context_override(args) -> str | None:
    """Get resume_context override from args."""
    if hasattr(args, "resume_context"):
        return args.resume_context
    return None


class ChatJob:
    """Job for the chat worker queue."""

    __slots__ = ("user", "show_prompt", "inject", "prompt_files")

    def __init__(
        self, *, user: str, show_prompt: bool, inject: list[str], prompt_files: list[str]
    ) -> None:
        self.user = user
        self.show_prompt = show_prompt
        self.inject = inject
        self.prompt_files = prompt_files


def _make_chunk_handler(agent_name: str, next_message_id: int):
    """Factory for streaming chunk handlers."""
    stream_started_ref = [False]
    stream_visible_chars_ref = [0]
    stream_truncated_ref = [False]

    def on_chunk(chunk: str) -> None:
        if not stream_started_ref[0]:
            sys.stdout.write(f"{GREEN}{agent_name}#{next_message_id}>{RESET} ")
            sys.stdout.flush()
            stream_started_ref[0] = True
        remaining = max(0, 900 - stream_visible_chars_ref[0])
        if remaining > 0:
            visible = chunk[:remaining]
            sys.stdout.write(visible)
            sys.stdout.flush()
            stream_visible_chars_ref[0] += len(visible)
        if remaining < len(chunk) and not stream_truncated_ref[0]:
            sys.stdout.write(
                f"{GRAY}[回复较长，后续内容已折叠。完成后可用 /expand last 查看全文。]{RESET}"
            )
            sys.stdout.flush()
            stream_truncated_ref[0] = True

    return on_chunk, stream_started_ref


def _render_assistant_response(text: str, assistant_outputs: list[str], agent_name: str) -> None:
    preview, collapsed = collapse_response_text(text)
    assistant_outputs.append(text)
    message_id = len(assistant_outputs)
    if collapsed:
        print(f"{GREEN}{agent_name}#{message_id}>{RESET} {preview}")
        print(
            f"{GRAY}[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]{RESET}"
        )
        return
    print(f"{GREEN}{agent_name}#{message_id}>{RESET} {text}")


def _handle_expand_command(raw: str, assistant_outputs: list[str]) -> None:
    target = parse_expand_target(raw)
    if target is None:
        print("用法: /expand [last|编号]")
        return
    if not assistant_outputs:
        print("当前没有可展开的助手回复。")
        return
    if target == "last":
        index = len(assistant_outputs)
    else:
        index = int(target)
    if index < 1 or index > len(assistant_outputs):
        print(f"没有编号为 {index} 的助手回复。当前共有 {len(assistant_outputs)} 条。")
        return
    print(f"===== ASSISTANT RESPONSE #{index} =====")
    print(assistant_outputs[index - 1])
    print("===== END RESPONSE =====")


def _read_user_input(state_lock: threading.Lock, fallback_waiting_for_input_ref: list) -> str:
    """Read a line of input, handling both tty and non-tty cases."""
    if sys.stdin.isatty():
        print(FALLBACK_CHAT_PROMPT, end="", flush=True)
        fallback_waiting_for_input_ref[0] = True
        try:
            return input().strip()
        finally:
            fallback_waiting_for_input_ref[0] = False
    return input(FALLBACK_CHAT_PROMPT).strip()


def _fallback_gateway_handle(
    job,
    agent,
    args,
    paths,
    assistant_outputs: list[str],
    build_history_context: Callable[[], str],
) -> tuple[str, bool]:
    """Handle gateway-mode job. Returns (response_text, stream_started)."""
    if not check_gateway_alive(paths):
        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
    history_ctx = build_history_context()
    turn_inject = (
        list(job.inject) + [_CHAT_RESPONSE_STYLE_INJECT] + ([history_ctx] if history_ctx else [])
    )
    next_message_id = len(assistant_outputs) + 1
    on_chunk, stream_started_ref = _make_chunk_handler(agent.config.agent_name, next_message_id)
    request_id, chunk_path, response_path = submit_chat_request(
        paths,
        prompt=job.user,
        inject=turn_inject,
        prompt_files=job.prompt_files,
        save=not args.no_save,
        show_prompt=job.show_prompt,
        resume_context=resume_context_override(args),
        agent=agent,
    )
    timeout = getattr(args, "gateway_timeout", None) or agent.config.gateway_request_timeout
    chunks_printed_ref = [0]
    deadline = time.time() + max(0.0, timeout)
    response = poll_gateway_chunks(
        chunk_path, response_path, deadline, on_chunk, chunks_printed_ref=chunks_printed_ref
    )
    if not response:
        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id}")
    return response.get("response", ""), stream_started_ref[0]


def _fallback_local_handle(
    job,
    agent,
    args,
    assistant_outputs: list[str],
    build_history_context: Callable[[], str],
) -> tuple[str, bool]:
    """Handle local-mode job. Returns (response_text, stream_started)."""
    history_ctx = build_history_context()
    turn_inject = (
        list(job.inject) + [_CHAT_RESPONSE_STYLE_INJECT] + ([history_ctx] if history_ctx else [])
    )
    next_message_id = len(assistant_outputs) + 1
    on_chunk, stream_started_ref = _make_chunk_handler(agent.config.agent_name, next_message_id)
    result = agent.run(
        job.user,
        inject=turn_inject,
        prompt_files=job.prompt_files,
        save=not args.no_save,
        source="chat",
        resume_context=resume_context_override(args),
        recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
        on_chunk=on_chunk,
    )
    agent_response_text = result.response
    if not stream_started_ref[0]:
        _render_assistant_response(agent_response_text, assistant_outputs, agent.config.agent_name)
    return agent_response_text, stream_started_ref[0]


def _fallback_worker(cfg: FallbackWorkerConfig) -> None:
    """Worker loop for fallback mode. Runs in a separate daemon thread."""
    while True:
        job = cfg.jobs.get()
        with cfg.state_lock:
            cfg.pending_jobs_ref[0] -= 1
            cfg.is_running_ref[0] = True
            cfg.running_prompt_ref[0] = job.user
            cfg.running_started_at_ref[0] = time.perf_counter()
        agent_response_text = ""
        stream_started = False
        try:
            started_at = cfg.running_started_at_ref[0]
            print(f"\n正在处理: {job.user}", flush=True)
            if cfg.use_gateway:
                agent_response_text, stream_started = _fallback_gateway_handle(
                    job,
                    cfg.agent,
                    cfg.args,
                    cfg.paths,
                    cfg.assistant_outputs,
                    cfg.build_history_context,
                )
            else:
                agent_response_text, stream_started = _fallback_local_handle(
                    job, cfg.agent, cfg.args, cfg.assistant_outputs, cfg.build_history_context
                )
        except Exception as exc:
            print(f"错误: {exc}")
        finally:
            if agent_response_text:
                if stream_started:
                    cfg.assistant_outputs.append(agent_response_text)
                append_conversation_turn(
                    cfg.conversation_history,
                    cfg.history_lock,
                    job.user,
                    agent_response_text,
                    max_turns=MAX_HISTORY_TURNS,
                )
            with cfg.state_lock:
                cfg.is_running_ref[0] = False
                cfg.running_prompt_ref[0] = ""
                cfg.running_started_at_ref[0] = 0.0
            cfg.jobs.task_done()


def _show_status(
    state_lock: threading.Lock,
    is_running_ref: list,
    pending_jobs_ref: list,
    running_prompt_ref: list,
    running_started_at_ref: list,
    agent,
    paths,
    use_gateway: bool,
) -> None:
    """Print status line."""
    with state_lock:
        active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
        prompt = running_prompt_ref[0]
        elapsed = time.perf_counter() - running_started_at_ref[0] if is_running_ref[0] else 0
    if not active:
        print("当前没有后台任务。")
    elif is_running_ref[0]:
        print(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {pending_jobs_ref[0]} 个任务。")
        print(f"当前任务: {prompt}")
    else:
        print(f"当前没有运行中的任务；队列中还有 {pending_jobs_ref[0]} 个任务。")
    if use_gateway:
        for line in render_gateway_status(agent, paths):
            print(line)


def _fallback_handle_command(cfg: FallbackHandleCommandConfig) -> bool:
    """Handle a single user command. Returns True if should exit."""
    if is_exit_command(cfg.user):
        shutting_down = [True]
        with cfg.state_lock:
            active = cfg.pending_jobs_ref[0] + (1 if cfg.is_running_ref[0] else 0)
        if active:
            print(f"还有 {active} 个后台任务，等待完成后退出。按 Ctrl+C 可强制退出。")
            cfg.jobs.join()
        print("再见。")
        return True
    if cfg.user == "/expand" or cfg.user.startswith("/expand "):
        _handle_expand_command(cfg.user, cfg.assistant_outputs)
        return False
    if cfg.user == "/status":
        _show_status(
            cfg.state_lock,
            cfg.is_running_ref,
            cfg.pending_jobs_ref,
            cfg.running_prompt_ref,
            cfg.running_started_at_ref,
            cfg.agent,
            cfg.paths,
            cfg.use_gateway,
        )
        return False
    if handle_common_slash_command(
        cfg.user,
        agent=cfg.agent,
        memory_limit=cfg.args.memory_limit,
        runtime_inject=cfg.runtime_inject,
        prompt_files=cfg.prompt_files,
        print_line=print,
        include_fallback_help=True,
    ):
        return False
    return None


def _fallback_enqueue_job(
    user: str,
    jobs: queue.Queue,
    state_lock: threading.Lock,
    pending_jobs_ref: list,
    runtime_inject_list: list[str],
    prompt_files: list[str],
) -> ChatJob:
    """Create and enqueue a chat job."""
    show_prompt, text = is_show_prompt_command(user)
    job = ChatJob(
        user=text,
        show_prompt=show_prompt,
        inject=list(runtime_inject_list),
        prompt_files=list(prompt_files),
    )
    with state_lock:
        pending_jobs_ref[0] += 1
    jobs.put(job)
    return job


def run_fallback(cfg: RunFallbackConfig) -> int:
    """Plain input()-based chat loop for terminals without prompt_toolkit."""

    is_running_ref = [False]
    pending_jobs_ref = [0]
    running_prompt_ref = [""]
    running_started_at_ref = [0.0]
    fallback_waiting_for_input_ref = [False]
    assistant_outputs: list[str] = []

    worker_cfg = FallbackWorkerConfig(
        jobs=cfg.jobs,
        state_lock=cfg.state_lock,
        is_running_ref=is_running_ref,
        pending_jobs_ref=pending_jobs_ref,
        running_prompt_ref=running_prompt_ref,
        running_started_at_ref=running_started_at_ref,
        agent=cfg.agent,
        args=cfg.args,
        paths=cfg.paths,
        use_gateway=cfg.use_gateway,
        assistant_outputs=assistant_outputs,
        conversation_history=cfg.conversation_history,
        history_lock=cfg.history_lock,
        build_history_context=cfg.build_history_context,
    )
    threading.Thread(
        target=_fallback_worker,
        args=(worker_cfg,),
        daemon=True,
    ).start()

    print(_startup_banner(cfg.agent.config.agent_name, use_gateway=cfg.use_gateway))
    print(
        f"{cfg.agent.config.agent_name} 交互循环已启动 [v2 fallback模式]。"
        "输入 /help 查看命令，输入 /exit 退出。"
    )
    if cfg.use_gateway:
        print("当前模式: gateway 客户端。普通消息会投递给后台 gateway 处理。")

    while True:
        try:
            user = _read_user_input(cfg.state_lock, fallback_waiting_for_input_ref)
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not user:
            continue

        handle_cfg = FallbackHandleCommandConfig(
            user=user,
            agent=cfg.agent,
            args=cfg.args,
            runtime_inject=cfg.runtime_inject,
            prompt_files=cfg.prompt_files,
            use_gateway=cfg.use_gateway,
            state_lock=cfg.state_lock,
            is_running_ref=is_running_ref,
            pending_jobs_ref=pending_jobs_ref,
            running_prompt_ref=running_prompt_ref,
            running_started_at_ref=running_started_at_ref,
            paths=cfg.paths,
            assistant_outputs=assistant_outputs,
            jobs=cfg.jobs,
        )
        exit_result = _fallback_handle_command(handle_cfg)
        if exit_result is not None:
            if exit_result:
                break
            continue

        _fallback_enqueue_job(
            user,
            cfg.jobs,
            cfg.state_lock,
            pending_jobs_ref,
            cfg.runtime_inject,
            cfg.prompt_files,
        )
        print(f"\n{terminal_rule()}")
        print(f"{BLUE}●{RESET}  {BLUE}{BOLD}{user}{RESET}")

    cfg.session_manager.touch_session(cfg.current_session_id, channel="chat")
    return 0


__all__ = [
    "FALLBACK_CHAT_PROMPT",
    "MAX_HISTORY_TURNS",
    "ChatJob",
    "append_conversation_turn",
    "run_fallback",
]
