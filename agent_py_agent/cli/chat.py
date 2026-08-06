

from __future__ import annotations

import queue
import sys
import threading

# Gateway imports
from ..agent.gateway_parts import (
    gateway_paths,
    render_gateway_status,
    wait_for_gateway_running,
)
from ..agent.memory_api import request_memory_curator_for_session_best_effort
from ..agent.session import SessionManager, generate_session_id

# Public symbols used by chat CLI tests and helpers.
from .chat_parts import (
    BLUE,
    BOLD,
    COLLAPSE_PREVIEW_CHARS,
    COLLAPSE_PREVIEW_LINES,
    CONTEXT_WINDOW,
    CYAN,
    GRAY,
    GREEN,
    MAX_HISTORY_TURNS,
    RESET,
    YELLOW,
    ChatJob,
    collapse_response_text,
    progress_bar,
    startup_banner,
    terminal_rule,
)

# Chat helper imports from chat_parts.
from .chat_parts.history import (
    append_conversation_turn,
    build_history_context,
    chat_assistant_preview_chars,
    chat_history_max_turns,
)
from .chat_parts.input_loop import handle_common_slash_command
from .chat_parts.plain import PLAIN_CHAT_PROMPT, run_plain
from .chat_parts.plain_state import RunPlainConfig
from .chat_parts.tui import TuiRunParams, run_tui
from .common import make_agent, resume_context_override
from .models import ChatJob
from .thinking_spinner import ThinkingSpinner

# Chat response style injected into CLI sessions.
_CHAT_RESPONSE_STYLE_INJECT = (
    "这是 CLI 聊天界面。回答风格要求："
    "1. 不要用模板化欢迎词；"
    "2. 不要在结尾主动列出'你可以问我这三个问题'这类建议问题；"
    "3. 直接围绕用户当前输入回答，除非用户要求，否则不要做教学式铺垫；"
    "4. 除非用户明确要求，不要先介绍你会什么、不要先列能力清单；"
    "5. 默认优先用简短自然语言回答，不要动不动列 1、2、3。"
)


def _setup_session(args, session_manager: SessionManager):
    if hasattr(args, "session_id") and args.session_id:
        session = session_manager.load_session(args.session_id)
        if session is None:
            print(f"会话 {args.session_id} 不存在，将创建新会话。", file=sys.stderr)
            session = session_manager.create_session(channel="chat")
        else:
            session_manager.touch_session(session.session_id, channel="chat")
            print(f"已恢复会话: {session.session_id}")
    else:
        session = session_manager.create_session(channel="chat")
        print(f"新会话: {session.session_id}")
    return session.session_id


def _init_chat_state(agent):
    conversation_history: list[tuple[str, str]] = []
    history_lock = threading.Lock()
    state = dict(
        conversation_history=conversation_history,
        history_lock=history_lock,
        jobs=queue.Queue(),
        state_lock=threading.Lock(),
        is_running=False,
        pending_jobs=0,
        shutting_down=False,
        running_prompt="",
        running_request_id="",
        running_started_at=0.0,
        last_token_estimate=0,
    )

    def _build_history_context() -> str:
        return build_history_context(
            conversation_history,
            history_lock,
            max_turns=chat_history_max_turns(agent.config),
            assistant_preview_chars=chat_assistant_preview_chars(agent.config),
        )

    return state, _build_history_context


def _has_prompt_toolkit() -> bool:
    try:
        from prompt_toolkit import PromptSession
        return PromptSession is not None and sys.stdin.isatty() and sys.stdout.isatty()
    except ImportError:
        return False


def _ensure_chat_memory_limit(args, agent) -> None:
    if getattr(args, "memory_limit", None) is None:
        args.memory_limit = int(getattr(agent.config, "cli_chat_memory_limit", 5) or 0)


# LLM: Local and Gateway chat share conversation/archive semantics; normal session exit only submits one best-effort Curator close reason.
# 函数用途: 启动交互聊天并在正常关闭后登记统一后台会话提炼请求。
def cmd_chat(args) -> int:
    agent = make_agent(args)
    _ensure_chat_memory_limit(args, agent)
    use_gateway = bool(args.gateway)
    paths = gateway_paths(agent)
    if use_gateway:
        _, alive = wait_for_gateway_running(
            paths,
            timeout=float(getattr(agent.config, "gateway_ready_timeout_seconds", 10) or 10),
        )
        if not alive:
            print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
            return 2

    session_manager = SessionManager(agent.config)
    current_session_id = _setup_session(args, session_manager)
    state, build_history_context = _init_chat_state(agent)
    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []

    if _has_prompt_toolkit() and not bool(getattr(args, "plain", False)):
        result = run_tui(params=TuiRunParams(
            agent=agent, args=args, use_gateway=use_gateway, paths=paths,
            runtime_inject=runtime_inject, prompt_files=prompt_files,
            conversation_history=state["conversation_history"],
            history_lock=state["history_lock"],
            jobs=state["jobs"], state_lock=state["state_lock"],
            is_running_ref=[state["is_running"]],
            pending_jobs_ref=[state["pending_jobs"]],
            shutting_down_ref=[state["shutting_down"]],
            running_prompt_ref=[state["running_prompt"]],
            running_request_id_ref=[state["running_request_id"]],
            running_started_at_ref=[state["running_started_at"]],
            last_token_estimate_ref=[state["last_token_estimate"]],
            build_history_context=build_history_context,
            session_manager=session_manager,
            current_session_id=current_session_id,
        ))
    else:
        result = run_plain(RunPlainConfig(
            agent=agent, args=args, use_gateway=use_gateway, paths=paths,
            runtime_inject=runtime_inject, prompt_files=prompt_files,
            conversation_history=state["conversation_history"],
            history_lock=state["history_lock"],
            jobs=state["jobs"], state_lock=state["state_lock"],
            build_history_context=build_history_context,
            session_manager=session_manager,
            current_session_id=current_session_id,
        ))
    request_memory_curator_for_session_best_effort(agent, event="close")
    return result


render_gateway_status = render_gateway_status
wait_for_gateway_running = wait_for_gateway_running
resume_context_override = resume_context_override
PLAIN_CHAT_PROMPT = PLAIN_CHAT_PROMPT
