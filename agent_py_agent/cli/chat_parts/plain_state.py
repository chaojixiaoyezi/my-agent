# LLM: 本模块集中定义 plain chat 的共享 DTO、任务对象和有界历史 helper；TUI 复用 ChatJob 时不得另造不兼容任务协议。
# 模块用途: 保存普通终端聊天所需的参数束、队列任务、历史常量和小型状态辅助函数。

from __future__ import annotations

"""plain chat state objects and small helpers.

给人看的解释：
这些类型和常量属于普通终端 chat 主链路；保留在一个状态模块里，避免多个薄壳文件来回跳转。
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


# LLM: worker 和输入端必须共享同一个 local_run_ref；句柄随 job 换代，不能从 agent 的线程局部参数猜身份。
# 类用途: 集中普通终端 worker 的队列、运行快照和本地控制句柄。
@dataclass
class PlainWorkerConfig:

    jobs: Any  # queue.Queue
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
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
    local_run_ref: list = field(default_factory=lambda: [None])


# LLM: 运行快照由 worker 在 state_lock 内更新，local_run_ref 仅传递本次调用的控制句柄。
# 类用途: 保存普通终端输入端与 worker 共享的状态引用。
@dataclass
class PlainInputRefs:

    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
    running_started_at_ref: list
    assistant_outputs: list[str]
    local_run_ref: list = field(default_factory=lambda: [None])


@dataclass
class PlainEnqueueParams:

    user: str
    jobs: Any
    state_lock: threading.Lock
    pending_jobs_ref: list
    runtime_inject_list: list[str]
    prompt_files: list[str]


# LLM: 命令使用与 worker 同一份运行引用；本地执行身份只从 local_run_ref 的正式发布读取。
# 类用途: 把普通终端当前输入及控制所需的依赖交给命令分派。
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
    running_request_id_ref: list
    running_started_at_ref: list
    paths: Any
    assistant_outputs: list[str]
    jobs: Any  # queue.Queue
    current_session_id: str = ""
    local_run_ref: list = field(default_factory=lambda: [None])


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


# LLM: ChatJob 是 plain/TUI worker 队列中的唯一任务对象；user 是模型正文，display_text 只保存用户原始输入用于队列编辑投影。
# 类用途: 携带一次待执行聊天输入、注入、附件 refs、请求身份和结构化系统任务。
class ChatJob:

    __slots__ = (
        "user",
        "show_prompt",
        "inject",
        "prompt_files",
        "request_id",
        "system_task",
        "display_text",
        "gateway_request_id",
        "client_message_id",
        "save",
        "resume_context",
        "tool_approval",
        "rich_transcript",
        "inject_complete",
        "input_media",
    )

    # LLM: display_text 缺省回落到 user，只影响可编辑队列显示；request_id/system_task 仍是执行与控制的机器事实。
    # 函数用途: 创建一条可由 plain 或 TUI worker 串行消费的聊天任务，冻结附件 refs。
    def __init__(
        self,
        *,
        user: str,
        show_prompt: bool,
        inject: list[str],
        prompt_files: list[str],
        request_id: str,
        system_task: dict[str, object] | None = None,
        display_text: str = "",
        gateway_request_id: str = "",
        client_message_id: str = "",
        save: bool = True,
        resume_context: bool | None = None,
        tool_approval: bool = False,
        rich_transcript: bool = False,
        inject_complete: bool = False,
        input_media: tuple[dict, ...] = (),
    ) -> None:
        self.user = user
        self.show_prompt = show_prompt
        self.inject = inject
        self.prompt_files = prompt_files
        self.request_id = request_id
        self.system_task = dict(system_task or {})
        self.display_text = str(display_text or user)
        self.gateway_request_id = str(gateway_request_id or "")
        self.client_message_id = str(client_message_id or "")
        self.save = bool(save)
        self.resume_context = resume_context if isinstance(resume_context, bool) else None
        self.tool_approval = bool(tool_approval)
        self.rich_transcript = bool(rich_transcript)
        self.inject_complete = bool(inject_complete)
        self.input_media = tuple(input_media)
