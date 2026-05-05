"""LLM: implements interactive chat mode with optional gateway client execution and background job queue.

给人看的解释：
chat 模式要一边接收用户输入，一边让模型在后台跑。
这个文件只处理交互体验、队列和内置斜杠命令，真正模型调用仍然走 SimpleAgent 或 gateway。
现在委托给 chat_parts 包中的专门模块。
"""

from __future__ import annotations

import queue
import sys
import threading

# Gateway imports
from ..agent.gateway import (
    gateway_paths,
    render_gateway_status,
    wait_for_gateway_running,
)
from ..agent.session import SessionManager, generate_session_id

# Re-export public symbols from chat_parts for backward compatibility
from .chat_parts import (
    BLUE,
    BOLD,
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
from .chat_parts.fallback import FALLBACK_CHAT_PROMPT, run_fallback

# Backward compatibility imports from chat_parts
from .chat_parts.history import append_conversation_turn, build_history_context
from .chat_parts.input_loop import handle_common_slash_command
from .chat_parts.rendering import (
    COLLAPSE_PREVIEW_CHARS as _COLLAPSE_PREVIEW_CHARS,
)
from .chat_parts.rendering import (
    COLLAPSE_PREVIEW_LINES as _COLLAPSE_PREVIEW_LINES,
)
from .chat_parts.rendering import (
    CONTEXT_WINDOW as _CONTEXT_WINDOW,
)
from .chat_parts.tui import TuiRunParams, run_tui
from .common import make_agent, resume_context_override
from .models import ChatJob
from .thinking_spinner import ThinkingSpinner

# Constants for backward compatibility
_CHAT_RESPONSE_STYLE_INJECT = (
    "这是 CLI 聊天界面。回答风格要求："
    "1. 不要用模板化欢迎词；"
    "2. 不要在结尾主动列出'你可以问我这三个问题'这类建议问题；"
    "3. 直接围绕用户当前输入回答，除非用户要求，否则不要做教学式铺垫；"
    "4. 除非用户明确要求，不要先介绍你会什么、不要先列能力清单；"
    "5. 默认优先用简短自然语言回答，不要动不动列 1、2、3。"
)


def _setup_session(args, session_manager: SessionManager):
    """解析 session_id 参数，创建或恢复会话并返回 session_id。"""
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


def _init_chat_state():
    """初始化聊天循环的共享状态，返回 (state_dict, build_fn)。"""
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
        running_started_at=0.0,
        last_token_estimate=0,
    )

    def _build_history_context() -> str:
        return build_history_context(conversation_history, history_lock, max_turns=MAX_HISTORY_TURNS)

    return state, _build_history_context


def _has_prompt_toolkit() -> bool:
    """检测 prompt_toolkit 是否可用且终端支持。"""
    try:
        from prompt_toolkit import PromptSession
        return PromptSession is not None and sys.stdin.isatty() and sys.stdout.isatty()
    except ImportError:
        return False


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

    session_manager = SessionManager(agent.config)
    current_session_id = _setup_session(args, session_manager)
    state, build_history_context = _init_chat_state()
    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []

    if _has_prompt_toolkit():
        return run_tui(params=TuiRunParams(
            agent=agent, args=args, use_gateway=use_gateway, paths=paths,
            runtime_inject=runtime_inject, prompt_files=prompt_files,
            conversation_history=state["conversation_history"],
            history_lock=state["history_lock"],
            jobs=state["jobs"], state_lock=state["state_lock"],
            is_running_ref=[state["is_running"]],
            pending_jobs_ref=[state["pending_jobs"]],
            shutting_down_ref=[state["shutting_down"]],
            running_prompt_ref=[state["running_prompt"]],
            running_started_at_ref=[state["running_started_at"]],
            last_token_estimate_ref=[state["last_token_estimate"]],
            build_history_context=build_history_context,
            session_manager=session_manager,
            current_session_id=current_session_id,
        ))
    return run_fallback(
        agent=agent, args=args, use_gateway=use_gateway, paths=paths,
        runtime_inject=runtime_inject, prompt_files=prompt_files,
        conversation_history=state["conversation_history"],
        history_lock=state["history_lock"],
        jobs=state["jobs"], state_lock=state["state_lock"],
        build_history_context=build_history_context,
        session_manager=session_manager,
        current_session_id=current_session_id,
    )


# Backward compatibility: re-export from chat_parts for tests
_collapse_response_text = collapse_response_text
_progress_bar = progress_bar
_startup_banner = startup_banner
_terminal_rule = terminal_rule
render_gateway_status = render_gateway_status
wait_for_gateway_running = wait_for_gateway_running
resume_context_override = resume_context_override
FALLBACK_CHAT_PROMPT = FALLBACK_CHAT_PROMPT