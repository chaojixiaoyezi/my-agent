
# LLM: 本模块显式传递同一 TUI 的参数；恢复标志只控制 readiness 后读历史，不创建另一套 session 或上下文。
# 模块用途: 集中保存聊天界面依赖与启动参数，避免跨模块共享隐式全局状态。

from __future__ import annotations

"""dataclass parameter bundles for chat TUI modules.

给人看的解释：
TUI 的函数参数很多，集中放在这里，主循环文件就只保留流程逻辑。
"""

import dataclasses
import threading

from .command_interaction import CommandInteraction


# LLM: 命令与 worker 共用 local_run_ref；插件透传原候选版本与独立交互引用，不能借用聊天控制或运行身份。
# 类用途: 保存 TUI 单次命令所需的状态和精确本地控制引用。
@dataclasses.dataclass(frozen=True)
class TuiHandleCommandParams:
    user: str
    agent: object
    args: object
    runtime_inject: list[str]
    prompt_files: list[str]
    use_gateway: bool
    paths: object
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
    running_started_at_ref: list
    shutting_down_ref: list
    stop_event: threading.Event
    assistant_outputs: list[str]
    current_session_id: str = ""
    local_run_ref: list = dataclasses.field(default_factory=lambda: [None])
    plugin_client: object | None = None
    plugin_revision: str = ""
    command_interaction: CommandInteraction | None = None


# LLM: 保持与界面同一份 queue/refs，local_run_ref 随 job 更新，不能在工厂中复制。
# 类用途: 传递 worker 执行消息与发布控制句柄所需的依赖。
@dataclasses.dataclass(frozen=True)
class WorkerConfigParams:
    jobs: object
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
    running_started_at_ref: list
    agent: object
    args: object
    paths: object
    use_gateway: bool
    conversation_history: list
    history_lock: threading.Lock
    build_history_context: object
    assistant_outputs: list[str]
    last_token_estimate_ref: list
    stop_event: threading.Event
    current_session_id: str = ""
    tui_runtime: object | None = None
    local_run_ref: list = dataclasses.field(default_factory=lambda: [None])


# LLM: 后台线程沿用顶层共享引用；本地句柄不得进入刷新线程或被重建。
# 类用途: 组装启动聊天 worker 和界面刷新所需的参数。
@dataclasses.dataclass(frozen=True)
class StartWorkerParams:
    app_ref: list
    refresh_stop: threading.Event
    jobs: object
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
    running_started_at_ref: list
    agent: object
    args: object
    paths: object
    use_gateway: bool
    conversation_history: list
    history_lock: threading.Lock
    build_history_context: object
    assistant_outputs: list[str]
    last_token_estimate_ref: list
    stop_event: threading.Event
    current_session_id: str = ""
    tui_runtime: object | None = None
    agent_navigation: object | None = None
    local_run_ref: list = dataclasses.field(default_factory=lambda: [None])


# LLM: UI 只持原 worker 控制引用，不能按 session 或 request 推导实际 run/attempt。
# 类用途: 向界面工厂传递展示、输入与精确本地控制依赖。
@dataclasses.dataclass(frozen=True)
class MakeTuiAppParams:
    agent: object
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_started_at_ref: list
    last_token_estimate_ref: list
    jobs: object
    pending_jobs_ref_for_enqueue: list
    runtime_inject: list[str]
    prompt_files: list[str]
    args: object
    use_gateway: bool
    paths: object
    assistant_outputs: list[str]
    shutting_down_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
    stop_event: threading.Event
    current_session_id: str = ""
    tui_runtime: object | None = None
    agent_navigation: object | None = None
    local_run_ref: list = dataclasses.field(default_factory=lambda: [None])


# LLM: 恢复标志只影响 Gateway 历史；local_run_ref 只为 direct 的当前 job 共享控制句柄，不储存第二份任务权威。
# 类用途: 把界面依赖与是否等待服务后恢复历史的启动要求显式交给 TUI。
@dataclasses.dataclass(frozen=True)
class TuiRunParams:
    agent: object
    args: object
    use_gateway: bool
    paths: object
    runtime_inject: list[str]
    prompt_files: list[str]
    conversation_history: list
    history_lock: threading.Lock
    jobs: object
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    shutting_down_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
    running_started_at_ref: list
    last_token_estimate_ref: list
    build_history_context: object
    session_manager: object
    current_session_id: str
    recovered_display_events: tuple[dict[str, object], ...] | None = None
    recovered_message_cursor: int = 0
    recovered_before_message_cursor: int = 0
    restore_session_history: bool = False
    local_run_ref: list = dataclasses.field(default_factory=lambda: [None])
