

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


def _setup_session(args, session_manager: SessionManager) -> str:
    """入口会话选择(双席 seq1968 技术项): 所有显式指定 session 的入口对
    不存在/跨用户 ID fail-closed——显式指定且不可恢复 → 返回 "" 表示失败
    (cmd_chat 据此退出非 0), 绝不静默创建新会话; 未指定才创建新随机会话。"""
    if hasattr(args, "session_id") and args.session_id:
        session = session_manager.load_session(args.session_id)
        if session is None:
            print(
                f"会话 {args.session_id} 不存在; 显式指定会话必须已存在"
                f"(不带 --session-id 启动会创建新会话)",
                file=sys.stderr,
            )
            return ""
        current_user = str(
            getattr(getattr(session_manager, "config", None), "user_id", "") or ""
        )
        if current_user and session.user_id and session.user_id != current_user:
            print(
                f"会话 {args.session_id} 属于其他用户({session.user_id}), 无权恢复",
                file=sys.stderr,
            )
            return ""
        session_manager.touch_session(session.session_id, channel="chat")
        print(f"已恢复会话: {session.session_id}")
        return session.session_id
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


# 函数用途: 显式 resume <session_id> 只连接指定会话(owner seq1943 语义③ +
# 双席 seq1958 fail-closed): session 不存在/无效 → 报错退出(非 0), 绝不
# 静默创建新会话或回退到其他历史。session/task/run/attempt 四层保持分开——
# 本命令只决定"接哪条会话", 会话下的任务续跑仍由共享权威(runtime.db)驱动。
def cmd_resume(args) -> int:
    from ..agent.session.manager import SessionManager

    session_id = str(getattr(args, "session_id", "") or "").strip()
    if not session_id:
        print(
            "用法: my-agent resume <session_id>  (会话不存在时不创建新会话)",
            file=sys.stderr,
        )
        return 3
    try:
        agent = make_agent(args)
        manager = SessionManager(agent.config)
    except Exception as exc:  # noqa: BLE001 初始化失败 fail-closed
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 3
    if not manager.session_exists(session_id):
        print(
            f"会话 {session_id} 不存在; resume 只连接已存在的会话"
            f"(用 my-agent chat 创建新会话)",
            file=sys.stderr,
        )
        return 3
    # 跨用户负例(双席 seq1966): session 归属校验——session 的 user_id 与
    # 当前用户不符 → fail-closed 拒绝(拿到别人 session_id 也不能恢复)。
    current_user = str(getattr(agent.config, "user_id", "") or "")
    session = manager.load_session(session_id)
    if session is not None and session.user_id and current_user \
            and session.user_id != current_user:
        print(
            f"会话 {session_id} 属于其他用户({session.user_id}), 无权恢复",
            file=sys.stderr,
        )
        return 3
    # 存在且归属匹配: 复用 chat 流程——_setup_session 会 load + touch 恢复
    # 该会话, 不会新建。make_agent 的重复构建是 CLI 单次启动成本, 可接受。
    return cmd_chat(args)


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
    if not current_session_id:
        # 显式指定 session 不存在/跨用户(fail-closed, 双席 seq1968)——已
        # 报错, 不进入交互循环。
        return 3
    state, build_history_context = _init_chat_state(agent)
    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []

    # P4 入口集成（2026-08-17）：TS TUI 优先（node + 产物可用时拉起 长期助手
    # 形态前端）；否则回退 Python TUI / plain（唯一回退，禁双 renderer 抢
    # stdin——TS TUI 独占终端直到退出）。
    from .chat_parts.tui_ts_launcher import try_launch_ts_tui

    ts_tui_started = False
    if not bool(getattr(args, "plain", False)):
        try:
            gateway_base = (
                f"http://127.0.0.1:{int(getattr(agent.config, 'gateway_port', 8420) or 8420)}"
            )
            ts_tui_started = try_launch_ts_tui(
                gateway_base_url=gateway_base,
                session_id=str(current_session_id or ""),
                model=str(getattr(agent.config, "model_name", "") or "unknown"),
            )
        except Exception:  # noqa: BLE001 launcher 任何失败都不阻断入口
            ts_tui_started = False
    if ts_tui_started:
        result = 0
    elif _has_prompt_toolkit() and not bool(getattr(args, "plain", False)):
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
