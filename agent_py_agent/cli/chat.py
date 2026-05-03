from __future__ import annotations

"""LLM: implements interactive chat mode with optional gateway client execution and background job queue.

给人看的解释：
chat 模式要一边接收用户输入，一边让模型在后台跑。
这个文件只处理交互体验、队列和内置斜杠命令，真正模型调用仍然走 SimpleAgent 或 gateway。
"""

import json
import queue
import re
import sys
import threading
import time
from html import escape

from ..agent.gateway import (
    gateway_chunk_path,
    gateway_paths,
    gateway_running,
    read_json_file,
    render_gateway_status,
    submit_gateway_ask,
    wait_for_gateway_response,
    wait_for_gateway_running,
)
from ..agent.session import SessionManager, generate_session_id
from .chat_parts.history import (
    MAX_HISTORY_TURNS as _MAX_HISTORY_TURNS,
)
from .chat_parts.history import (
    append_conversation_turn,
    build_history_context,
)
from .chat_parts.rendering import (
    BLUE as _BLUE,
)
from .chat_parts.rendering import (
    BOLD as _BOLD,
)
from .chat_parts.rendering import (
    COLLAPSE_PREVIEW_CHARS as _COLLAPSE_PREVIEW_CHARS,
)
from .chat_parts.rendering import (
    COLLAPSE_PREVIEW_LINES as _COLLAPSE_PREVIEW_LINES,
)
from .chat_parts.rendering import (
    CONTEXT_WINDOW as _CONTEXT_WINDOW,
)
from .chat_parts.rendering import (
    CYAN as _CYAN,
)
from .chat_parts.rendering import (
    GRAY as _GRAY,
)
from .chat_parts.rendering import (
    GREEN as _GREEN,
)
from .chat_parts.rendering import (
    RESET as _RESET,
)
from .chat_parts.rendering import (
    YELLOW as _YELLOW,
)
from .chat_parts.rendering import (
    collapse_response_text as _collapse_response_text,
)
from .chat_parts.rendering import (
    progress_bar as _progress_bar,
)
from .chat_parts.rendering import (
    startup_banner as _startup_banner,
)
from .chat_parts.rendering import (
    terminal_rule as _terminal_rule,
)
from .chat_parts.slash_commands import handle_common_slash_command
from .common import CHAT_PROMPT, FALLBACK_CHAT_PROMPT, make_agent, resume_context_override
from .models import ChatJob
from .thinking_spinner import ThinkingSpinner

try:
    from prompt_toolkit import PromptSession
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
    PromptSession = None

_CHAT_RESPONSE_STYLE_INJECT = (
    "这是 CLI 聊天界面。回答风格要求："
    "1. 不要用模板化欢迎词；"
    "2. 不要在结尾主动列出'你可以问我这三个问题'这类建议问题；"
    "3. 直接围绕用户当前输入回答，除非用户要求，否则不要做教学式铺垫；"
    "4. 除非用户明确要求，不要先介绍你会什么、不要先列能力清单；"
    "5. 默认优先用简短自然语言回答，不要动不动列 1、2、3。"
)

def _cprint(text: str) -> None:
    """Print ANSI-colored text through prompt_toolkit's native renderer.

    Raw ANSI escapes written via print() are swallowed by patch_stdout's
    StdoutProxy.  Routing through print_formatted_text(ANSI(...)) lets
    prompt_toolkit parse the escapes and render real colors.
    """
    _pt_print(_PT_ANSI(text))


def cmd_chat(args) -> int:
    """启动交互循环。"""

    agent = make_agent(args)
    use_gateway = bool(args.gateway)
    paths = gateway_paths(agent)
    if use_gateway:
        _, alive = wait_for_gateway_running(paths, timeout=10.0)
        if not alive:
            print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
            return 2

    # 会话管理
    from ..agent.session import SessionManager, generate_session_id
    session_manager = SessionManager(agent.config)

    if hasattr(args, "session_id") and args.session_id:
        # 恢复已有会话
        session = session_manager.load_session(args.session_id)
        if session is None:
            print(f"会话 {args.session_id} 不存在，将创建新会话。", file=sys.stderr)
            session = session_manager.create_session(channel="chat")
        else:
            session_manager.touch_session(session.session_id, channel="chat")
            print(f"已恢复会话: {session.session_id}")
    else:
        # 创建新会话
        session = session_manager.create_session(channel="chat")
        print(f"新会话: {session.session_id}")

    current_session_id = session.session_id

    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []
    conversation_history: list[tuple[str, str]] = []
    history_lock = threading.Lock()
    jobs: queue.Queue[ChatJob] = queue.Queue()
    state_lock = threading.Lock()
    is_running = False
    pending_jobs = 0
    shutting_down = False
    running_prompt = ""
    running_started_at = 0.0
    last_token_estimate = 0

    def _build_history_context() -> str:
        return build_history_context(
            conversation_history,
            history_lock,
            max_turns=_MAX_HISTORY_TURNS,
        )

    # ── prompt_toolkit mode ──────────────────────────────────────────────

    if PromptSession is not None and sys.stdin.isatty() and sys.stdout.isatty():
        return _run_tui(
            agent=agent,
            args=args,
            use_gateway=use_gateway,
            paths=paths,
            runtime_inject=runtime_inject,
            prompt_files=prompt_files,
            conversation_history=conversation_history,
            history_lock=history_lock,
            jobs=jobs,
            state_lock=state_lock,
            is_running_ref=[is_running],
            pending_jobs_ref=[pending_jobs],
            shutting_down_ref=[shutting_down],
            running_prompt_ref=[running_prompt],
            running_started_at_ref=[running_started_at],
            last_token_estimate_ref=[last_token_estimate],
            build_history_context=_build_history_context,
        )

    # ── fallback mode (no prompt_toolkit) ────────────────────────────────

    return _run_fallback(
        agent=agent,
        args=args,
        use_gateway=use_gateway,
        paths=paths,
        runtime_inject=runtime_inject,
        prompt_files=prompt_files,
        conversation_history=conversation_history,
        history_lock=history_lock,
        jobs=jobs,
        state_lock=state_lock,
        build_history_context=_build_history_context,
    )


# ============================================================================
# prompt_toolkit mode — Application(full_screen=False) + patch_stdout
# ============================================================================


def _run_tui(
    *,
    agent,
    args,
    use_gateway,
    paths,
    runtime_inject,
    prompt_files,
    conversation_history,
    history_lock,
    jobs,
    state_lock,
    is_running_ref,
    pending_jobs_ref,
    shutting_down_ref,
    running_prompt_ref,
    running_started_at_ref,
    last_token_estimate_ref,
    build_history_context,
) -> int:
    """Application(full_screen=False) TUI following the Hermes pattern.

    - Status bar at top (model, context, thinking state)
    - Input area at bottom
    - patch_stdout() + _cprint() for colored streaming output
    - Line-buffered streaming (accumulate chunks, emit complete lines)
    """

    assistant_outputs: list[str] = []
    thinking_line_ref = [""]
    stop_event = threading.Event()
    history_file = agent.root / ".chat_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    stream_buf_ref = [""]  # partial line buffer for line-buffered streaming
    app_ref = [None]  # Application reference for invalidate()

    def _get_status_text() -> str:
        with state_lock:
            active = pending_jobs_ref[0] + (1 if is_running_ref[0] else 0)
            tokens = last_token_estimate_ref[0]
            started_at = running_started_at_ref[0]
        model = agent.config.model_name
        pct = tokens / _CONTEXT_WINDOW if _CONTEXT_WINDOW else 0
        bar = _progress_bar(pct)
        pieces = [
            f"⚕ {model}",
            f"ctx {tokens / 1000:.1f}K/{_CONTEXT_WINDOW / 1000:.0f}K",
            f"[{bar}] {pct:.0%}",
        ]
        # 碎碎念 + 读秒紧跟在用量后面
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
        return " │ ".join(pieces)

    def _print_banner() -> None:
        _cprint(f"{_CYAN}███╗   ███╗██╗   ██╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗{_RESET}")
        _cprint(f"{_CYAN}████╗ ████║╚██╗ ██╔╝      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝{_RESET}")
        _cprint(f"{_CYAN}██╔████╔██║ ╚████╔╝ █████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║{_RESET}")
        _cprint(f"{_CYAN}██║╚██╔╝██║  ╚██╔╝  ╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║{_RESET}")
        _cprint(f"{_CYAN}██║ ╚═╝ ██║   ██║         ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║{_RESET}")
        _cprint(f"{_CYAN}╚═╝     ╚═╝   ╚═╝         ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝{_RESET}")
        _cprint("")
        mode = "gateway" if use_gateway else "local"
        _cprint(f"╭──────────────────────── {_BOLD}{agent.config.agent_name}{_RESET} · {mode} ────────────────────────╮")
        _cprint("│ Welcome to my-agent. Type your message or /help for commands.")
        _cprint("╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────╯")
        _cprint("")

    def _render_user_entry(text: str) -> None:
        """Render user message with blue color and a separator."""
        lines = [line.rstrip() for line in text.splitlines()] or [text]
        _cprint(f"\n{_terminal_rule()}")
        for index, line in enumerate(lines):
            ball = f"{_BLUE}●{_RESET}" if index == 0 else " "
            _cprint(f"{ball}  {_BLUE}{_BOLD}{line}{_RESET}")
        _cprint("")
        _cprint("")

    def _render_assistant_response(text: str) -> None:
        preview, collapsed = _collapse_response_text(text)
        assistant_outputs.append(text)
        message_id = len(assistant_outputs)
        _cprint(f"{_GREEN}{agent.config.agent_name}#{message_id}>{_RESET} {preview if collapsed else text}")
        if collapsed:
            _cprint(f"{_GRAY}[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]{_RESET}")

    def _handle_expand_command(raw: str) -> bool:
        if raw == "/expand":
            target = "last"
        else:
            target = raw[len("/expand "):].strip()
        if not assistant_outputs:
            _cprint("当前没有可展开的助手回复。")
            return True
        if not target or target == "last":
            index = len(assistant_outputs)
        elif target.isdigit():
            index = int(target)
        else:
            _cprint("用法: /expand [last|编号]")
            return True
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
        if user.lower() in {"/exit", "/logout", "/quit", "exit", "logout", "退出"}:
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
        # Strip prefix "╭ 蛐蛐人："
        cleaned = text.replace("╭ 蛐蛐人：", "").strip()
        # Strip trailing elapsed time like "3.2s" — the status bar renders its own
        cleaned = re.sub(r"\s+\d+\.\d+s$", "", cleaned)
        thinking_line_ref[0] = cleaned

    # ── line-buffered streaming helpers ──────────────────────────────────

    def _emit_stream_line(text: str) -> None:
        """Emit a single line of stream text with the assistant prefix color."""
        _cprint(f"{_GREEN}{text}{_RESET}")

    def _flush_stream_buf() -> None:
        """Emit any remaining partial line from the stream buffer."""
        buf = stream_buf_ref[0]
        if buf:
            _emit_stream_line(buf)
            stream_buf_ref[0] = ""

    def _append_stream_text(chunk: str) -> None:
        """Accumulate chunk text, emitting complete lines (line-buffered)."""
        if not chunk:
            return
        stream_buf_ref[0] += chunk
        while "\n" in stream_buf_ref[0]:
            line, stream_buf_ref[0] = stream_buf_ref[0].split("\n", 1)
            _emit_stream_line(line)

    # ── worker thread ───────────────────────────────────────────────────

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
                def _on_spinner_update(text: str) -> None:
                    _set_thinking_line(text)
                    if app_ref[0] is not None:
                        app_ref[0].invalidate()

                spinner = ThinkingSpinner(
                    on_update=_on_spinner_update,
                    on_stop=lambda: (_set_thinking_line(""), app_ref[0].invalidate() if app_ref[0] else None),
                )
                spinner.start()

                def _begin_stream() -> None:
                    nonlocal stream_started
                    if stream_started:
                        return
                    spinner.stop()
                    _cprint(f"\n{_GREEN}{agent.config.agent_name}#{next_message_id}>{_RESET}")
                    stream_started = True

                def _on_stream_chunk(chunk: str) -> None:
                    nonlocal stream_has_visible_text
                    if not chunk or not chunk.strip():
                        return
                    _begin_stream()
                    stream_has_visible_text = True
                    _append_stream_text(chunk)

                if use_gateway:
                    _, alive = gateway_running(paths)
                    if not alive:
                        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
                    request_id, _, response_path = submit_gateway_ask(
                        paths,
                        prompt=job.user,
                        inject=turn_inject,
                        prompt_files=job.prompt_files,
                        save=not args.no_save,
                        include_prompt=job.show_prompt,
                        resume_context=resume_context_override(args),
                        agent=agent,
                    )
                    timeout = args.gateway_timeout if args.gateway_timeout is not None else agent.config.gateway_request_timeout
                    chunk_path = gateway_chunk_path(paths, request_id)
                    chunks_printed = 0
                    deadline = time.time() + max(0.0, timeout)
                    response = {}
                    while time.time() <= deadline:
                        if chunk_path.exists():
                            try:
                                lines = chunk_path.read_text(encoding="utf-8").splitlines()
                                for cline in lines[chunks_printed:]:
                                    if not cline.strip():
                                        continue
                                    cobj = json.loads(cline)
                                    chunk_text = cobj.get("text", "")
                                    if chunk_text:
                                        _on_stream_chunk(chunk_text)
                                    chunks_printed += 1
                            except (OSError, json.JSONDecodeError):
                                pass
                        response = read_json_file(response_path)
                        if response:
                            break
                        time.sleep(0.1)
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
                        f"{_GRAY}[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
                        f"工具轮数 {response.get('tool_rounds', 0)}; "
                        f"prompt_tokens≈{response.get('prompt_token_estimate', 0)}; "
                        f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]{_RESET}"
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
                            on_chunk=_on_stream_chunk,
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
                        f"{_GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
                        f"prompt_tokens≈{result.prompt_token_estimate}; "
                        f"resume_context={1 if result.memory_resume_context_injected else 0}]{_RESET}"
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
                    append_conversation_turn(
                        conversation_history,
                        history_lock,
                        job.user,
                        agent_response_text,
                        max_turns=_MAX_HISTORY_TURNS,
                    )
                with state_lock:
                    is_running_ref[0] = False
                    running_prompt_ref[0] = ""
                    running_started_at_ref[0] = 0.0
                thinking_line_ref[0] = ""
                stream_buf_ref[0] = ""
                jobs.task_done()

    def enqueue_job(user: str, *, show_prompt: bool = False) -> None:
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

    # ── prompt_toolkit Application layout ────────────────────────────────

    # Status bar at the top — dynamically updates via get_status_fragments
    status_bar = Window(
        content=FormattedTextControl(
            lambda: [("class:status-bar", f" {_get_status_text()} ")]
        ),
        height=1,
        style="class:status-bar",
    )

    # Input area at the bottom
    input_area = TextArea(
        height=Dimension(min=1, max=8),
        prompt=[("class:prompt", "❯ ")],
        style="class:input-area",
        multiline=False,
        wrap_lines=False,
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
    )

    # Key bindings
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        """Accept input and submit to the worker queue."""
        text = input_area.text.strip()
        if not text:
            return
        input_area.text = ""
        if handle_command(text):
            if stop_event.is_set():
                event.app.exit()
            return
        show_prompt = False
        if text.startswith("/show-prompt "):
            show_prompt = True
            text = text[len("/show-prompt "):]
        enqueue_job(text, show_prompt=show_prompt)

    @kb.add("c-c")
    def _(event):
        """Exit on Ctrl+C."""
        _request_exit()
        event.app.exit()

    @kb.add("c-d")
    def _(event):
        """Exit on Ctrl+D (EOF)."""
        _request_exit()
        event.app.exit()

    layout = Layout(
        HSplit([
            status_bar,
            Window(height=1),  # spacer
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

    # ── periodic status-bar refresh (1s) for elapsed-time ticking ────────
    refresh_stop = threading.Event()

    def _refresh_loop() -> None:
        while not refresh_stop.wait(1.0):
            if app_ref[0] is not None:
                app_ref[0].invalidate()

    refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)

    # ── start and run ────────────────────────────────────────────────────

    threading.Thread(target=worker, daemon=True).start()
    refresh_thread.start()

    # Print banner before the Application takes over the terminal
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


# ============================================================================
# fallback mode — stdlib input() based, no prompt_toolkit
# ============================================================================


def _run_fallback(
    *,
    agent,
    args,
    use_gateway,
    paths,
    runtime_inject,
    prompt_files,
    conversation_history,
    history_lock,
    jobs,
    state_lock,
    build_history_context,
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
        preview, collapsed = _collapse_response_text(text)
        assistant_outputs.append(text)
        message_id = len(assistant_outputs)
        if collapsed:
            print(f"{_GREEN}{agent.config.agent_name}#{message_id}>{_RESET} {preview}")
            print(f"{_GRAY}[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]{_RESET}")
            return
        print(f"{_GREEN}{agent.config.agent_name}#{message_id}>{_RESET} {text}")

    def handle_expand_command(raw: str) -> None:
        if raw == "/expand":
            target = "last"
        else:
            target = raw[len("/expand "):].strip()
        if not assistant_outputs:
            print("当前没有可展开的助手回复。")
            return
        if not target or target == "last":
            index = len(assistant_outputs)
        elif target.isdigit():
            index = int(target)
        else:
            print("用法: /expand [last|编号]")
            return
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
                stream_started = False
                stream_visible_chars = 0
                stream_truncated = False
                next_message_id = len(assistant_outputs) + 1
                if use_gateway:
                    _, alive = gateway_running(paths)
                    if not alive:
                        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
                    request_id, _, response_path = submit_gateway_ask(
                        paths,
                        prompt=job.user,
                        inject=turn_inject,
                        prompt_files=job.prompt_files,
                        save=not args.no_save,
                        include_prompt=job.show_prompt,
                        resume_context=resume_context_override(args),
                        agent=agent,
                    )
                    timeout = (
                        args.gateway_timeout
                        if args.gateway_timeout is not None
                        else agent.config.gateway_request_timeout
                    )
                    chunk_path = gateway_chunk_path(paths, request_id)
                    chunks_printed = 0
                    deadline = time.time() + max(0.0, timeout)
                    response = {}
                    while time.time() <= deadline:
                        if chunk_path.exists():
                            try:
                                lines = chunk_path.read_text(encoding="utf-8").splitlines()
                                for cline in lines[chunks_printed:]:
                                    if not cline.strip():
                                        continue
                                    cobj = json.loads(cline)
                                    chunk_text = cobj.get("text", "")
                                    if chunk_text:
                                        if not stream_started:
                                            sys.stdout.write(f"{_GREEN}{agent.config.agent_name}#{next_message_id}>{_RESET} ")
                                            sys.stdout.flush()
                                            stream_started = True
                                        remaining = max(0, _COLLAPSE_PREVIEW_CHARS - stream_visible_chars)
                                        if remaining > 0:
                                            visible = chunk_text[:remaining]
                                            sys.stdout.write(visible)
                                            sys.stdout.flush()
                                            stream_visible_chars += len(visible)
                                        if remaining < len(chunk_text) and not stream_truncated:
                                            sys.stdout.write(
                                                f"\n\n{_GRAY}[回复较长，后续内容已折叠。完成后可用 /expand last 查看全文。]{_RESET}"
                                            )
                                            sys.stdout.flush()
                                            stream_truncated = True
                                    chunks_printed += 1
                            except (OSError, json.JSONDecodeError):
                                pass
                        response = read_json_file(response_path)
                        if response:
                            break
                        time.sleep(0.1)
                    elapsed = time.perf_counter() - started_at
                    if stream_started:
                        print()
                    if not response:
                        raise TimeoutError(
                            f"gateway 请求等待超时: request_id={request_id} response={response_path}"
                        )
                    if job.show_prompt and response.get("prompt"):
                        print("===== FINAL PROMPT =====")
                        print(response.get("prompt", ""))
                        print("===== RESPONSE =====")
                    print(
                        f"{_GRAY}[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
                        f"工具轮数 {response.get('tool_rounds', 0)}; "
                        f"prompt_tokens≈{response.get('prompt_token_estimate', 0)}; "
                        f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]{_RESET}"
                    )
                    if response.get("ok"):
                        agent_response_text = response.get("response", "")
                        if not stream_started:
                            render_assistant_response(agent_response_text)
                    else:
                        print(f"错误: {response.get('error', 'gateway 请求失败')}")
                else:
                    def _on_chat_chunk(chunk: str) -> None:
                        nonlocal stream_started, stream_visible_chars, stream_truncated
                        if not stream_started:
                            sys.stdout.write(f"{_GREEN}{agent.config.agent_name}#{next_message_id}>{_RESET} ")
                            sys.stdout.flush()
                            stream_started = True
                        remaining = max(0, _COLLAPSE_PREVIEW_CHARS - stream_visible_chars)
                        if remaining > 0:
                            visible = chunk[:remaining]
                            sys.stdout.write(visible)
                            sys.stdout.flush()
                            stream_visible_chars += len(visible)
                        if remaining < len(chunk) and not stream_truncated:
                            sys.stdout.write(
                                f"\n\n{_GRAY}[回复较长，后续内容已折叠。完成后可用 /expand last 查看全文。]{_RESET}"
                            )
                            sys.stdout.flush()
                            stream_truncated = True

                    result = agent.run(
                        job.user,
                        inject=turn_inject,
                        prompt_files=job.prompt_files,
                        save=not args.no_save,
                        source="chat",
                        resume_context=resume_context_override(args),
                        recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
                        on_chunk=_on_chat_chunk,
                    )
                    elapsed = time.perf_counter() - started_at
                    if stream_started:
                        print()
                    if job.show_prompt:
                        print("===== FINAL PROMPT =====")
                        print(result.prompt)
                        print("===== RESPONSE =====")
                    print(
                        f"{_GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
                        f"prompt_tokens≈{result.prompt_token_estimate}; "
                        f"resume_context={1 if result.memory_resume_context_injected else 0}]{_RESET}"
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

    while True:
        try:
            if sys.stdin.isatty():
                print(FALLBACK_CHAT_PROMPT, end="", flush=True)
                with state_lock:
                    fallback_waiting_for_input = True
                try:
                    user = input().strip()
                finally:
                    with state_lock:
                        fallback_waiting_for_input = False
            else:
                user = input(FALLBACK_CHAT_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not user:
            continue
        if user.lower() in {"/exit", "/logout", "/quit", "exit", "logout", "退出"}:
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

        show_prompt = False
        if user.startswith("/show-prompt "):
            show_prompt = True
            user = user[len("/show-prompt "):]

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
        print(f"\n{_terminal_rule()}")
        print(f"{_BLUE}●{_RESET}  {_BLUE}{_BOLD}{user}{_RESET}")

    # 保存会话
    session_manager.touch_session(current_session_id, channel="chat")

    return 0
