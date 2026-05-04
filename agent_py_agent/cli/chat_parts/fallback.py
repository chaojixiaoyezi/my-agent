"""LLM: fallback worker for non-streaming responses in chat mode.

给人看的解释：
当终端不支持 prompt_toolkit 时，用纯 stdlib input() 的回退循环。
保持和 TUI 模式相同的功能，只是实现更简单。
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
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


def run_fallback(
    *,
    agent,
    args,
    use_gateway: bool,
    paths,
    runtime_inject: list[str],
    prompt_files: list[str],
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    jobs,  # queue.Queue
    state_lock: threading.Lock,
    build_history_context: Callable[[], str],
    session_manager,
    current_session_id: str,
) -> int:
    """Plain input()-based chat loop for terminals without prompt_toolkit."""

    is_running = False
    pending_jobs = 0
    shutting_down = False
    running_prompt = ""
    running_started_at = 0.0
    last_token_estimate = 0
    fallback_waiting_for_input = False
    assistant_outputs: list[str] = []

    def render_assistant_response(text: str) -> None:
        preview, collapsed = collapse_response_text(text)
        assistant_outputs.append(text)
        message_id = len(assistant_outputs)
        if collapsed:
            print(f"{GREEN}{agent.config.agent_name}#{message_id}>{RESET} {preview}")
            print(f"{GRAY}[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]{RESET}")
            return
        print(f"{GREEN}{agent.config.agent_name}#{message_id}>{RESET} {text}")

    def handle_expand_command(raw: str) -> None:
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

    def redraw_fallback_prompt() -> None:
        if not sys.stdin.isatty():
            return
        with state_lock:
            should = fallback_waiting_for_input and not shutting_down
        if should:
            print(FALLBACK_CHAT_PROMPT, end="", flush=True)

    def _make_chunk_handler(agent_name: str, next_message_id: int):
        """Factory for streaming chunk handlers — defined once outside if/else to avoid nesting depth 5."""
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

    def worker() -> None:
        nonlocal is_running, pending_jobs, running_prompt, running_started_at
        while True:
            job = jobs.get()
            with state_lock:
                pending_jobs -= 1
                is_running = True
                running_prompt = job.user
                running_started_at = time.perf_counter()
            try:
                started_at = running_started_at
                print(f"\n正在处理: {job.user}", flush=True)
                history_ctx = build_history_context()
                turn_inject = list(job.inject) + [_CHAT_RESPONSE_STYLE_INJECT] + ([history_ctx] if history_ctx else [])
                agent_response_text = ""
                next_message_id = len(assistant_outputs) + 1
                on_chunk, stream_started_ref = _make_chunk_handler(
                    agent.config.agent_name, next_message_id
                )
                if use_gateway:
                    if not check_gateway_alive(paths):
                        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
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
                    elapsed = time.perf_counter() - started_at
                    if stream_started_ref[0]:
                        print()
                    if not response:
                        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id}")
                    if job.show_prompt and response.get("prompt"):
                        print("===== FINAL PROMPT =====")
                        print(response.get("prompt", ""))
                        print("===== RESPONSE =====")
                    print(
                        f"{GRAY}[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
                        f"工具轮数 {response.get('tool_rounds', 0)}; "
                        f"prompt_tokens~{response.get('prompt_token_estimate', 0)}; "
                        f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]{RESET}"
                    )
                    if response.get("ok"):
                        agent_response_text = response.get("response", "")
                    else:
                        print(f"错误: {response.get('error', 'gateway 请求失败')}")
                        agent_response_text = ""
                else:
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
                    elapsed = time.perf_counter() - started_at
                    stream_started = get_stream_started()
                    if stream_started:
                        print()
                    if job.show_prompt:
                        print("===== FINAL PROMPT =====")
                        print(result.prompt)
                        print("===== RESPONSE =====")
                    print(
                        f"{GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
                        f"prompt_tokens~{result.prompt_token_estimate}; "
                        f"resume_context={1 if result.memory_resume_context_injected else 0}]{RESET}"
                    )
                    agent_response_text = result.response
                    if not stream_started:
                        render_assistant_response(agent_response_text)
            except Exception as exc:
                print(f"错误: {exc}")
            finally:
                if agent_response_text:
                    if stream_started:
                        assistant_outputs.append(agent_response_text)
                    append_conversation_turn(
                        conversation_history,
                        history_lock,
                        job.user,
                        agent_response_text,
                        max_turns=_MAX_HISTORY_TURNS,
                    )
                with state_lock:
                    is_running = False
                    running_prompt = ""
                    running_started_at = 0.0
                jobs.task_done()
                redraw_fallback_prompt()

    threading.Thread(target=worker, daemon=True).start()

    print(_startup_banner(agent.config.agent_name, use_gateway=use_gateway))
    print(
        f"{agent.config.agent_name} 交互循环已启动 [v2 fallback模式]。"
        "输入 /help 查看命令，输入 /exit 退出。"
    )
    if use_gateway:
        print("当前模式: gateway 客户端。普通消息会投递给后台 gateway 处理。")

    def _read_user_input() -> str:
        """Read a line of input, handling both tty and non-tty cases."""
        if sys.stdin.isatty():
            print(FALLBACK_CHAT_PROMPT, end="", flush=True)
            with state_lock:
                fallback_waiting_for_input = True
            try:
                return input().strip()
            finally:
                with state_lock:
                    fallback_waiting_for_input = False
        return input(FALLBACK_CHAT_PROMPT).strip()

    while True:
        try:
            user = _read_user_input()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not user:
            continue
        if is_exit_command(user):
            shutting_down = True
            with state_lock:
                active = pending_jobs + (1 if is_running else 0)
            if active:
                print(f"还有 {active} 个后台任务，等待完成后退出。按 Ctrl+C 可强制退出。")
                jobs.join()
            print("再见。")
            return 0
        if user == "/expand" or user.startswith("/expand "):
            handle_expand_command(user)
            continue
        if user == "/status":
            with state_lock:
                active = pending_jobs + (1 if is_running else 0)
                prompt = running_prompt
                elapsed = time.perf_counter() - running_started_at if is_running else 0
            if not active:
                print("当前没有后台任务。")
            elif is_running:
                print(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {pending_jobs} 个任务。")
                print(f"当前任务: {prompt}")
            else:
                print(f"当前没有运行中的任务；队列中还有 {pending_jobs} 个任务。")
            if use_gateway:
                for line in render_gateway_status(agent, paths):
                    print(line)
            continue
        if handle_common_slash_command(
            user,
            agent=agent,
            memory_limit=args.memory_limit,
            runtime_inject=runtime_inject,
            prompt_files=prompt_files,
            print_line=print,
            include_fallback_help=True,
        ):
            continue

        show_prompt, user = is_show_prompt_command(user)

        job = ChatJob(
            user=user,
            show_prompt=show_prompt,
            inject=list(runtime_inject),
            prompt_files=list(prompt_files),
        )
        with state_lock:
            active = pending_jobs + (1 if is_running else 0)
            pending_jobs += 1
        jobs.put(job)
        print(f"\n{terminal_rule()}")
        print(f"{BLUE}●{RESET}  {BLUE}{BOLD}{user}{RESET}")

    # 保存会话
    session_manager.touch_session(current_session_id, channel="chat")

    return 0


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

    def __init__(self, *, user: str, show_prompt: bool, inject: list[str], prompt_files: list[str]) -> None:
        self.user = user
        self.show_prompt = show_prompt
        self.inject = inject
        self.prompt_files = prompt_files


__all__ = [
    "FALLBACK_CHAT_PROMPT",
    "MAX_HISTORY_TURNS",
    "ChatJob",
    "append_conversation_turn",
    "run_fallback",
]