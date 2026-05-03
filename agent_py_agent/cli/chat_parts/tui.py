"""LLM: TUI input loop and prompt_toolkit integration for chat mode.

给人看的解释：
prompt_toolkit 的 Application 封装、状态栏刷新、worker 线程管理，
都在这里实现，让 chat.py 的主循环保持简洁。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from .gateway_client import (
    check_gateway_alive,
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
    CYAN,
    GRAY,
    GREEN,
    RESET,
    collapse_response_text,
    progress_bar,
    startup_banner,
    terminal_rule,
)
from .session_state import ConversationHistory

try:
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.application import Application
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import TextArea
except ImportError:  # pragma: no cover
    _pt_print = None
    Application = None

# Constants
_CHAT_RESPONSE_STYLE_INJECT = (
    "这是 CLI 聊天界面。回答风格要求："
    "1. 不要用模板化欢迎词；"
    "2. 不要在结尾主动列出'你可以问我这三个问题'这类建议问题；"
    "3. 直接围绕用户当前输入回答，除非用户要求，否则不要做教学式铺垫；"
    "4. 除非用户明确要求，不要先介绍你会什么、不要先列能力清单；"
    "5. 默认优先用简短自然语言回答，不要动不动列 1、2、3。"
)

MAX_HISTORY_TURNS = 8
CONTEXT_WINDOW = 200_000
COLLAPSE_PREVIEW_CHARS = 900


def _cprint(text: str) -> None:
    """Print ANSI-colored text through prompt_toolkit's native renderer."""
    if _pt_print is not None:
        _pt_print(_PT_ANSI(text))


def run_tui(
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
    is_running_ref: list,
    pending_jobs_ref: list,
    shutting_down_ref: list,
    running_prompt_ref: list,
    running_started_at_ref: list,
    last_token_estimate_ref: list,
    build_history_context: Callable[[], str],
    session_manager,
    current_session_id: str,
) -> int:
    """Application(full_screen=False) TUI following the Hermes pattern."""

    assistant_outputs: list[str] = []
    thinking_line_ref = [""]
    stop_event = threading.Event()
    history_file = agent.root / ".chat_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    stream_buf_ref = [""]
    app_ref = [None]

    def _get_status_text() -> str:
        with state_lock:
            active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
            tokens = last_token_estimate_ref[0]
            started_at = running_started_at_ref[0]
        model = agent.config.model_name
        pct = tokens / CONTEXT_WINDOW if CONTEXT_WINDOW else 0
        bar = progress_bar(pct)
        pieces = [
            f"⚕ {model}",
            f"ctx {tokens / 1000:.1f}K/{CONTEXT_WINDOW / 1000:.0f}K",
            f"[{bar}] {pct:.0%}",
        ]
        if thinking_line_ref[0]:
            pieces.append(thinking_line_ref[0])
            if started_at:
                elapsed = max(0.0, time.perf_counter() - started_at)
                pieces.append(f"{elapsed:.0f}s")
        elif active:
            elapsed = max(0.0, time.perf_counter() - started_at) if started_at else 0.0
            pieces.append(f"{elapsed:.0f}s")
        else:
            pieces.append("空闲")
        with state_lock:
            if pending_jobs_ref[0]:
                pieces.append(f"队列 {pending_jobs_ref[0]}")
        return " | ".join(pieces)

    def _print_banner() -> None:
        _cprint(f"{CYAN}███╗   ███╗██╗   ██╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗{RESET}")
        _cprint(f"{CYAN}████╗ ████║╚██╗ ██╔╝      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝{RESET}")
        _cprint(f"{CYAN}██╔████╔██║ ╚████╔╝ █████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║{RESET}")
        _cprint(f"{CYAN}██║╚██╔╝██║  ╚██╔╝  ╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║{RESET}")
        _cprint(f"{CYAN}██║ ╚═╝ ██║   ██║         ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║{RESET}")
        _cprint(f"{CYAN}╚═╝     ╚═╝   ╚═╝         ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝{RESET}")
        _cprint("")
        mode = "gateway" if use_gateway else "local"
        _cprint(f"╭──────────────────────── {BOLD}{agent.config.agent_name}{RESET} · {mode} ────────────────────────╮")
        _cprint("│ Welcome to my-agent. Type your message or /help for commands.")
        _cprint("╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────╯")
        _cprint("")

    def _render_user_entry(text: str) -> None:
        lines = [line.rstrip() for line in text.splitlines()] or [text]
        _cprint(f"\n{terminal_rule()}")
        for index, line in enumerate(lines):
            ball = f"{BLUE}●{RESET}" if index == 0 else " "
            _cprint(f"{ball}  {BLUE}{BOLD}{line}{RESET}")
        _cprint("")
        _cprint("")

    def _render_assistant_response(text: str) -> None:
        preview, collapsed = collapse_response_text(text)
        assistant_outputs.append(text)
        message_id = len(assistant_outputs)
        _cprint(f"{GREEN}{agent.config.agent_name}#{message_id}>{RESET} {preview if collapsed else text}")
        if collapsed:
            _cprint(f"{GRAY}[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]{RESET}")

    def _handle_expand_command(raw: str) -> bool:
        target = parse_expand_target(raw)
        if target is None:
            _cprint("用法: /expand [last|编号]")
            return True
        if not assistant_outputs:
            _cprint("当前没有可展开的助手回复。")
            return True
        if target == "last":
            index = len(assistant_outputs)
        else:
            index = int(target)
        if index < 1 or index > len(assistant_outputs):
            _cprint(f"没有编号为 {index} 的助手回复。当前共有 {len(assistant_outputs)} 条。")
            return True
        _cprint(f"===== ASSISTANT RESPONSE #{index} =====")
        _cprint(assistant_outputs[index - 1])
        _cprint("===== END RESPONSE =====")
        return True

    def _request_exit() -> None:
        shutting_down_ref[0] = True
        with state_lock:
            active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
        if active:
            _cprint(f"还有 {active} 个后台任务，等待完成后退出。")
        stop_event.set()

    def handle_command(user: str) -> bool:
        if is_exit_command(user):
            _request_exit()
            return True
        if user == "/expand" or user.startswith("/expand "):
            return _handle_expand_command(user)
        if user == "/status":
            with state_lock:
                active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
                prompt = running_prompt_ref[0]
                elapsed = time.perf_counter() - running_started_at_ref[0] if is_running_ref[0] else 0
            if not active:
                _cprint("当前没有后台任务。")
            elif is_running_ref[0]:
                _cprint(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {pending_jobs_ref[0]} 个任务。")
                _cprint(f"当前任务: {prompt}")
            else:
                _cprint(f"当前没有运行中的任务；队列中还有 {pending_jobs_ref[0]} 个任务。")
            if use_gateway:
                from ...agent.gateway import render_gateway_status
                for line in render_gateway_status(agent, paths):
                    _cprint(line)
            return True
        return handle_common_slash_command(
            user,
            agent=agent,
            memory_limit=args.memory_limit,
            runtime_inject=runtime_inject,
            prompt_files=prompt_files,
            print_line=_cprint,
        )

    def _set_thinking_line(text: str) -> None:
        if not text:
            thinking_line_ref[0] = ""
            return
        cleaned = text.replace("╭ 蛐蛐人：", "").strip()
        cleaned = re.sub(r"\s+\d+\.\d+s$", "", cleaned)
        thinking_line_ref[0] = cleaned

    def _emit_stream_line(text: str) -> None:
        _cprint(f"{GREEN}{text}{RESET}")

    def _flush_stream_buf() -> None:
        buf = stream_buf_ref[0]
        if buf:
            _emit_stream_line(buf)
            stream_buf_ref[0] = ""

    def _append_stream_text(chunk: str) -> None:
        if not chunk:
            return
        stream_buf_ref[0] += chunk
        while "\n" in stream_buf_ref[0]:
            line, stream_buf_ref[0] = stream_buf_ref[0].split("\n", 1)
            _emit_stream_line(line)

    def worker() -> None:
        while not stop_event.is_set():
            try:
                job = jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            with state_lock:
                pending_jobs_ref[0] -= 1
                is_running_ref[0] = True
                running_prompt_ref[0] = job.user
                running_started_at_ref[0] = time.perf_counter()
            try:
                started_at = running_started_at_ref[0]
                history_ctx = build_history_context()
                turn_inject = list(job.inject) + [_CHAT_RESPONSE_STYLE_INJECT] + ([history_ctx] if history_ctx else [])
                agent_response_text = ""
                response_recorded = False
                stream_started = False
                stream_has_visible_text = False
                next_message_id = len(assistant_outputs) + 1

                def on_spinner_update(text: str) -> None:
                    _set_thinking_line(text)
                    if app_ref[0] is not None:
                        app_ref[0].invalidate()

                from .thinking_spinner import ThinkingSpinner
                spinner = ThinkingSpinner(
                    on_update=on_spinner_update,
                    on_stop=lambda: (_set_thinking_line(""), app_ref[0].invalidate() if app_ref[0] else None),
                )
                spinner.start()

                def begin_stream() -> None:
                    nonlocal stream_started
                    if stream_started:
                        return
                    spinner.stop()
                    _cprint(f"\n{GREEN}{agent.config.agent_name}#{next_message_id}>{RESET}")
                    stream_started = True

                def on_stream_chunk(chunk: str) -> None:
                    nonlocal stream_has_visible_text
                    if not chunk or not chunk.strip():
                        return
                    begin_stream()
                    stream_has_visible_text = True
                    _append_stream_text(chunk)

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
                    timeout = args.gateway_timeout if args.gateway_timeout is not None else agent.config.gateway_request_timeout
                    chunks_printed_ref = [0]
                    deadline = time.time() + max(0.0, timeout)
                    response = poll_gateway_chunks(
                        chunk_path, response_path, deadline, on_stream_chunk, chunks_printed_ref=chunks_printed_ref
                    )
                    spinner.stop()
                    elapsed = time.perf_counter() - started_at
                    if stream_started:
                        _flush_stream_buf()
                    if not response:
                        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id} response={response_path}")
                    if job.show_prompt and response.get("prompt"):
                        _cprint("===== FINAL PROMPT =====")
                        _cprint(response.get("prompt", ""))
                        _cprint("===== RESPONSE =====")
                    _cprint(
                        f"{GRAY}[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
                        f"工具轮数 {response.get('tool_rounds', 0)}; "
                        f"prompt_tokens~{response.get('prompt_token_estimate', 0)}; "
                        f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]{RESET}"
                    )
                    if response.get("ok"):
                        agent_response_text = response.get("response", "")
                        with state_lock:
                            last_token_estimate_ref[0] = response.get("prompt_token_estimate", 0)
                        if (not stream_has_visible_text) and agent_response_text.strip():
                            _render_assistant_response(agent_response_text)
                            response_recorded = True
                    else:
                        _cprint(f"错误: {response.get('error', 'gateway 请求失败')}")
                else:
                    try:
                        result = agent.run(
                            job.user,
                            inject=turn_inject,
                            prompt_files=job.prompt_files,
                            save=not args.no_save,
                            source="chat",
                            resume_context=resume_context_override(args),
                            recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
                            on_chunk=on_stream_chunk,
                        )
                    finally:
                        spinner.stop()
                    elapsed = time.perf_counter() - started_at
                    if stream_started:
                        _flush_stream_buf()
                    if job.show_prompt:
                        _cprint("===== FINAL PROMPT =====")
                        _cprint(result.prompt)
                        _cprint("===== RESPONSE =====")
                    _cprint(
                        f"{GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
                        f"prompt_tokens~{result.prompt_token_estimate}; "
                        f"resume_context={1 if result.memory_resume_context_injected else 0}]{RESET}"
                    )
                    agent_response_text = result.response
                    with state_lock:
                        last_token_estimate_ref[0] = result.prompt_token_estimate
                    if (not stream_has_visible_text) and agent_response_text.strip():
                        _render_assistant_response(agent_response_text)
                        response_recorded = True
            except Exception as exc:
                _set_thinking_line("")
                _cprint(f"错误: {exc}")
                agent_response_text = ""
            finally:
                if agent_response_text:
                    if not response_recorded:
                        assistant_outputs.append(agent_response_text)
                    _append_conversation_turn(
                        conversation_history,
                        history_lock,
                        job.user,
                        agent_response_text,
                    )
                with state_lock:
                    is_running_ref[0] = False
                    running_prompt_ref[0] = ""
                    running_started_at_ref[0] = 0.0
                thinking_line_ref[0] = ""
                stream_buf_ref[0] = ""
                jobs.task_done()

    def enqueue_job(user: str, *, show_prompt: bool = False) -> None:
        from .fallback import ChatJob
        job = ChatJob(
            user=user,
            show_prompt=show_prompt,
            inject=list(runtime_inject),
            prompt_files=list(prompt_files),
        )
        with state_lock:
            pending_jobs_ref[0] += 1
        jobs.put(job)
        _render_user_entry(user)

    # prompt_toolkit Application layout
    status_bar = Window(
        content=FormattedTextControl(
            lambda: [("class:status-bar", f" {_get_status_text()} ")]
        ),
        height=1,
        style="class:status-bar",
    )

    input_area = TextArea(
        height=Dimension(min=1, max=8),
        prompt=[("class:prompt", "❯ ")],
        style="class:input-area",
        multiline=False,
        wrap_lines=False,
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
    )

    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        text = input_area.text.strip()
        if not text:
            return
        input_area.text = ""
        if handle_command(text):
            if stop_event.is_set():
                event.app.exit()
            return
        show_prompt, text = is_show_prompt_command(text)
        enqueue_job(text, show_prompt=show_prompt)

    @kb.add("c-c")
    def _(event):
        _request_exit()
        event.app.exit()

    @kb.add("c-d")
    def _(event):
        _request_exit()
        event.app.exit()

    layout = Layout(
        HSplit([
            status_bar,
            Window(height=1),
            input_area,
        ])
    )

    style = Style.from_dict({
        "status-bar": "bg:#1a1a2e #8ec5ff bold",
        "input-area": "#f8fafc",
        "prompt": "#f8fafc bold",
    })

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        mouse_support=False,
    )
    app_ref[0] = app

    # periodic status-bar refresh
    refresh_stop = threading.Event()

    def _refresh_loop() -> None:
        while not refresh_stop.wait(1.0):
            if app_ref[0] is not None:
                app_ref[0].invalidate()

    refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)

    # start and run
    threading.Thread(target=worker, daemon=True).start()
    refresh_thread.start()

    _print_banner()

    try:
        with patch_stdout():
            app.run()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        refresh_stop.set()

    stop_event.set()
    _cprint("\n再见。")

    # 保存会话
    session_manager.touch_session(current_session_id, channel="chat")

    return 0


def _append_conversation_turn(
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


def resume_context_override(args) -> str | None:
    """Get resume_context override from args."""
    if hasattr(args, "resume_context"):
        return args.resume_context
    return None


__all__ = [
    "MAX_HISTORY_TURNS",
    "CONTEXT_WINDOW",
    "COLLAPSE_PREVIEW_CHARS",
    "run_tui",
]