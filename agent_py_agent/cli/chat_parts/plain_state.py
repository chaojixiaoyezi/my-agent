
from __future__ import annotations

"""plain chat state objects and small helpers.

给人看的解释：
这些类型和常量属于普通终端 chat 主链路；保留在一个状态模块里，避免多个薄壳文件来回跳转。
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class PlainWorkerConfig:

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
    current_session_id: str = ""


@dataclass
class WorkerStateRefs:

    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list


@dataclass
class PlainInputRefs:

    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    assistant_outputs: list[str]


@dataclass
class PlainEnqueueParams:

    user: str
    jobs: Any
    state_lock: threading.Lock
    pending_jobs_ref: list
    runtime_inject_list: list[str]
    prompt_files: list[str]


@dataclass
class PlainHandleCommandConfig:

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
class RunPlainConfig:

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

PLAIN_CHAT_PROMPT = "❯ "
MAX_HISTORY_TURNS = 20
ASSISTANT_PREVIEW_CHARS = 500


def append_conversation_turn(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    turn: ConversationTurn,
    *,
    max_turns: int = MAX_HISTORY_TURNS,
) -> None:
    with history_lock:
        conversation_history.append((turn.user_message, turn.assistant_message))
        if len(conversation_history) > max_turns:
            conversation_history[:] = conversation_history[-max_turns:]


def render_gateway_status(agent, paths):
    from ...agent.gateway_parts import render_gateway_status as _rgs

    return _rgs(agent, paths)


def resume_context_override(args) -> str | None:
    if hasattr(args, "resume_context"):
        return args.resume_context
    return None


class ChatJob:

    __slots__ = ("user", "show_prompt", "inject", "prompt_files")

    def __init__(
        self, *, user: str, show_prompt: bool, inject: list[str], prompt_files: list[str]
    ) -> None:
        self.user = user
        self.show_prompt = show_prompt
        self.inject = inject
        self.prompt_files = prompt_files
