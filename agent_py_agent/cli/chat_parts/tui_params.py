
# LLM: 本模块显式传递同一 TUI 的参数；恢复标志只控制 readiness 后读历史，不创建另一套 session 或上下文。
# 模块用途: 集中保存聊天界面依赖与启动参数，避免跨模块共享隐式全局状态。

from __future__ import annotations

"""dataclass parameter bundles for chat TUI modules.

给人看的解释：
TUI 的函数参数很多，集中放在这里，主循环文件就只保留流程逻辑。
"""

import dataclasses
import threading


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


# LLM: 恢复标志要求 Gateway preflight 后读取 exact session；模型预览和只读事件分离，显示历史不能送入 jobs 或模型请求。
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
    restore_session_history: bool = False
