# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


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
from .chat_parts.fallback_state import RunFallbackConfig

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


# LLM: _setup_session 处理 chat session 选择；恢复和新建路径都在这里分流。
# 函数用途: 根据 session_id 加载历史会话，否则创建新 session 并返回 id。
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


# LLM: _init_chat_state 初始化 chat 共享状态；TUI 和 fallback 共用这些字段。
# 函数用途: 创建对话历史、锁、停止事件、队列和 history context builder。
def _init_chat_state():
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

    # LLM: _build_history_context 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
    def _build_history_context() -> str:
        return build_history_context(conversation_history, history_lock, max_turns=MAX_HISTORY_TURNS)

    return state, _build_history_context


# LLM: _has_prompt_toolkit 决定是否走 TUI；导入失败时回落到 fallback。
# 函数用途: 探测 prompt_toolkit 是否可用，避免缺依赖时中断 chat 命令。
def _has_prompt_toolkit() -> bool:
    try:
        from prompt_toolkit import PromptSession
        return PromptSession is not None and sys.stdin.isatty() and sys.stdout.isatty()
    except ImportError:
        return False


# LLM: cmd_chat 是交互聊天入口；会在 gateway、本地、TUI 和 fallback 间选择路径。
# 函数用途: 创建 agent/session，注入响应风格，并启动对应的聊天界面。
def cmd_chat(args) -> int:
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

    if _has_prompt_toolkit() and not bool(getattr(args, "plain", False)):
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
    return run_fallback(RunFallbackConfig(
        agent=agent, args=args, use_gateway=use_gateway, paths=paths,
        runtime_inject=runtime_inject, prompt_files=prompt_files,
        conversation_history=state["conversation_history"],
        history_lock=state["history_lock"],
        jobs=state["jobs"], state_lock=state["state_lock"],
        build_history_context=build_history_context,
        session_manager=session_manager,
        current_session_id=current_session_id,
    ))


# Backward compatibility: re-export from chat_parts for tests
_collapse_response_text = collapse_response_text
_progress_bar = progress_bar
_startup_banner = startup_banner
_terminal_rule = terminal_rule
render_gateway_status = render_gateway_status
wait_for_gateway_running = wait_for_gateway_running
resume_context_override = resume_context_override
FALLBACK_CHAT_PROMPT = FALLBACK_CHAT_PROMPT
