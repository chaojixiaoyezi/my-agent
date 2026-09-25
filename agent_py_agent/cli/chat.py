

from __future__ import annotations

import queue
import sys
import threading

# Public symbols used by chat CLI tests and helpers. Import the renderer leaf directly; importing
# chat_parts.__init__ would eagerly load Gateway adapters and duplicate the TUI/plain runtime.
from .chat_parts.renderer import (
    BLUE,
    BOLD,
    COLLAPSE_PREVIEW_CHARS,
    COLLAPSE_PREVIEW_LINES,
    CONTEXT_WINDOW,
    CYAN,
    GRAY,
    GREEN,
    RESET,
    YELLOW,
    collapse_response_text,
    progress_bar,
    startup_banner,
    terminal_rule,
)
from .models import ChatJob
from .thinking_spinner import ThinkingSpinner

# LLM: Keep module import lightweight. Every helper below preserves the former public patch point
# while importing its implementation only when that path is actually used.
# 模块用途: 编排聊天会话；Gateway TUI 使用轻量客户端快速首屏，普通终端和 direct 模式仍按需
# 加载完整智能体。

MAX_HISTORY_TURNS = 20
PLAIN_CHAT_PROMPT = "user> "

# Chat response style injected into CLI sessions.
_CHAT_RESPONSE_STYLE_INJECT = (
    "这是 CLI 聊天界面。回答风格要求："
    "1. 不要用模板化欢迎词；"
    "2. 不要在结尾主动列出'你可以问我这三个问题'这类建议问题；"
    "3. 直接围绕用户当前输入回答，除非用户要求，否则不要做教学式铺垫；"
    "4. 除非用户明确要求，不要先介绍你会什么、不要先列能力清单；"
    "5. 默认优先用简短自然语言回答，不要动不动列 1、2、3。"
)


# LLM: Tests and non-interactive commands may patch this public function. The structured marker is
# set only after TTY/gateway routing is decided and never from natural-language input.
# 函数用途: 按聊天入口类型创建轻量 Gateway 客户端或完整智能体。
def make_agent(args):
    if getattr(args, "_gateway_client_mode", False) is True:
        from .chat_client_context import make_gateway_chat_client

        return make_gateway_chat_client(args)
    from .common import make_agent as _make_agent

    return _make_agent(args)


# LLM: Lazy wrapper preserves the historic chat module symbol without importing Gateway runtime
# during CLI argument parsing or the immediate boot frame.
# 函数用途: 根据轻量客户端或完整智能体计算 Gateway 文件协议路径。
def gateway_paths(agent):
    from ..agent.gateway_parts.paths import gateway_paths as _gateway_paths

    return _gateway_paths(agent)


# LLM: Status rendering remains owned by gateway_parts; this wrapper is compatibility-only.
# 函数用途: 按需加载并展示 Gateway 状态。
def render_gateway_status(agent, paths):
    from ..agent.gateway_parts.status_rendering import render_gateway_status as _render

    return _render(agent, paths)


# LLM: Readiness remains the canonical monotonic Gateway wait; wrapper keeps startup imports small.
# 函数用途: 按需等待 Gateway 在限定时间内就绪。
def wait_for_gateway_running(paths, timeout: float = 3.0):
    from ..agent.gateway_parts.status_rendering import wait_for_gateway_running as _wait

    return _wait(paths, timeout=timeout)


# LLM: Factory-shaped compatibility symbol allows existing tests to patch SessionManager while
# avoiding session/runtime-error imports before the TUI route is selected.
# 函数用途: 按需创建当前配置对应的会话管理器。
def SessionManager(config):  # noqa: N802 - compatibility with the former imported class
    from ..agent.session.manager import SessionManager as _SessionManager

    return _SessionManager(config)


# LLM: History helpers stay canonical in chat_parts.history and are loaded only after client setup.
# 函数用途: 按配置构造本地 direct 模式的最近对话上下文。
def build_history_context(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    *,
    max_turns: int = 20,
    assistant_preview_chars: int = 500,
) -> str:
    from .chat_parts.history import build_history_context as _build

    return _build(
        conversation_history,
        history_lock,
        max_turns=max_turns,
        assistant_preview_chars=assistant_preview_chars,
    )


# LLM: Compatibility wrapper for config-derived history limits.
# 函数用途: 读取当前聊天最多保留多少轮历史。
def chat_history_max_turns(config) -> int:
    from .chat_parts.history import chat_history_max_turns as _limit

    return _limit(config)


# LLM: Compatibility wrapper for config-derived assistant preview size.
# 函数用途: 读取历史上下文中助手回复的最大预览字符数。
def chat_assistant_preview_chars(config) -> int:
    from .chat_parts.history import chat_assistant_preview_chars as _limit

    return _limit(config)


# LLM: Explicit resume alone loads the ConversationStore history projection; new sessions skip it.
# 函数用途: 按需恢复指定 Gateway 会话的完整问答历史。
def load_gateway_chat_history(
    agent: object,
    session_id: str,
    *,
    max_turns: int,
):
    from .chat_parts.history import load_gateway_chat_history as _load

    return _load(agent, session_id, max_turns=max_turns)


# LLM: Plain and TUI runners stay patchable at this module boundary for focused tests.
# 函数用途: 按需加载并运行普通终端聊天循环。
def run_plain(config):
    from .chat_parts.plain import run_plain as _run_plain

    return _run_plain(config)


# LLM: TUI imports occur only after lightweight config/session setup and remain on the main thread.
# 函数用途: 按需加载并运行 prompt_toolkit 全屏聊天界面。
def run_tui(*, params):
    from .chat_parts.tui import run_tui as _run_tui

    return _run_tui(params=params)


# LLM: Full-agent close semantics retain the canonical memory API; lightweight clients use the
# Gateway lifecycle contract in _request_chat_session_close instead.
# 函数用途: 为完整智能体的会话关闭登记一次 best-effort 记忆策展请求。
def request_memory_curator_for_session_best_effort(agent, *, event: str) -> bool:
    from ..agent.memory_api import request_memory_curator_for_session_best_effort as _request

    return _request(agent, event=event)


# LLM: Preserve the common CLI helper without importing SimpleAgent at chat module load time.
# 函数用途: 读取命令参数中是否临时覆盖恢复上下文开关。
def resume_context_override(args) -> bool | None:
    return getattr(args, "resume_context", None)


def _setup_session(args, session_manager) -> str:
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


# LLM: 显式恢复只读 canonical history；Gateway TUI 将读取交给既有 preflight，不能在 readiness 前 HTTP 请求或以空历史启动 worker。
# 函数用途: 启动或恢复聊天，本地直接读历史，薄客户端先显示连接界面；正常关闭后登记统一会话提炼请求。
def cmd_chat(args) -> int:
    from .chat_parts.tui_upgrade_follow import adopt_handoff, release_quiet_streams

    # 原地切换来的新进程接着用上一代的会话；普通启动只记下终端设置，供以后切换时带过去。
    handoff = adopt_handoff()
    if handoff.child and not str(getattr(args, "session_id", "") or "").strip():
        args.session_id = handoff.session_id
    use_gateway = bool(args.gateway)
    use_tui = _has_prompt_toolkit() and not bool(getattr(args, "plain", False))
    args._gateway_client_mode = bool(use_gateway and use_tui)
    agent = make_agent(args)
    _ensure_chat_memory_limit(args, agent)
    paths = gateway_paths(agent)
    if use_gateway and not use_tui:
        _, alive = wait_for_gateway_running(
            paths,
            timeout=float(getattr(agent.config, "gateway_ready_timeout_seconds", 3) or 3),
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
    recovered_display_events = None
    recovered_message_cursor = 0
    recovered_before_message_cursor = 0
    resume_session = bool(str(getattr(args, "session_id", "") or "").strip())
    # 原地切换时旧画面还留在屏幕上：先同步确认 Gateway 就绪并读好历史，新界面首帧就是同一段对话；没就绪才退回可见的连接流程。
    handoff_ready = bool(handoff.child and use_gateway and use_tui and _handoff_gateway_ready(agent, paths))
    deferred_history = bool(resume_session and use_gateway and use_tui and not handoff_ready)
    if resume_session and not deferred_history:
        restored = load_gateway_chat_history(
            agent,
            current_session_id,
            max_turns=chat_history_max_turns(agent.config),
        )
        if restored.load_errors:
            print("会话历史读取失败，未进入聊天。", file=sys.stderr)
            return 3
        state["conversation_history"].extend(restored.turns)
        recovered_display_events = restored.display_events
        recovered_message_cursor = restored.message_cursor
        recovered_before_message_cursor = restored.before_message_cursor
    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []

    if use_tui:
        from .chat_parts.tui_params import TuiRunParams

        release_quiet_streams()
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
            recovered_display_events=recovered_display_events,
            recovered_message_cursor=recovered_message_cursor,
            recovered_before_message_cursor=recovered_before_message_cursor,
            restore_session_history=deferred_history,
            handoff=handoff_ready,
        ))
    else:
        from .chat_parts.plain_state import RunPlainConfig

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
    _request_chat_session_close(agent, current_session_id)
    return result


# LLM: 只给原地切换用：旧画面还在屏幕上，可以同步等；读 Gateway 自己的就绪事实，不看文案。没就绪返回 False，调用方退回可见连接流程。
# 函数用途: 原地切换的新进程在首帧之前确认 Gateway 已可用。
def _handoff_gateway_ready(agent, paths) -> bool:
    from .chat_parts.tui_preflight import wait_for_gateway_readiness

    try:
        outcome = wait_for_gateway_readiness(
            paths, timeout=float(getattr(agent.config, "gateway_ready_timeout_seconds", 3) or 3),
        )
    except (OSError, ValueError, TypeError):
        return False
    return bool(getattr(outcome, "ready", False))


# LLM: Lightweight clients must signal the already-running Gateway rather than promoting to a full
# local agent during exit; full/direct clients retain the existing Memory API call.
# 函数用途: 在退出聊天后登记会话关闭，轻量和完整客户端各走其唯一权威入口。
def _request_chat_session_close(agent, session_id: str) -> bool:
    if getattr(agent, "gateway_client_only", False) is True:
        return bool(agent.request_session_lifecycle(session_id, event="close"))
    return request_memory_curator_for_session_best_effort(agent, event="close")
