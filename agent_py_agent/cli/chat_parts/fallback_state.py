# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

"""fallback chat state objects and small compatibility helpers.

给人看的解释：
这些类型和常量被 fallback / TUI worker 复用，拆出来避免 fallback 主循环文件过长。
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


# LLM: FallbackWorkerConfig 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass
class FallbackWorkerConfig:

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


# LLM: FallbackHandleCommandConfig 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass
class FallbackHandleCommandConfig:

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


# LLM: RunFallbackConfig 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass
class RunFallbackConfig:

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


# LLM: ConversationTurn 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass(frozen=True)
class ConversationTurn:
    user_message: str
    assistant_message: str


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


# LLM: _startup_banner 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _startup_banner(agent_name: str, *, use_gateway: bool) -> str:
    from .rendering import startup_banner as _sb

    return _sb(agent_name, use_gateway=use_gateway)


# LLM: append_conversation_turn 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def append_conversation_turn(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    turn: ConversationTurn,
    *,
    max_turns: int = MAX_HISTORY_TURNS,
) -> None:
    with history_lock:
        conversation_history.append((turn.user_message, turn.assistant_message))
        if len(conversation_history) > max_turns * 2:
            conversation_history[:] = conversation_history[-max_turns:]


# LLM: render_gateway_status 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def render_gateway_status(agent, paths):
    from ...agent.gateway import render_gateway_status as _rgs

    return _rgs(agent, paths)


# LLM: resume_context_override 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def resume_context_override(args) -> str | None:
    if hasattr(args, "resume_context"):
        return args.resume_context
    return None


# LLM: ChatJob 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class ChatJob:

    __slots__ = ("user", "show_prompt", "inject", "prompt_files")

    # LLM: __init__ 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(
        self, *, user: str, show_prompt: bool, inject: list[str], prompt_files: list[str]
    ) -> None:
        self.user = user
        self.show_prompt = show_prompt
        self.inject = inject
        self.prompt_files = prompt_files

