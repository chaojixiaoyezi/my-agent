from __future__ import annotations

"""LLM: dataclass parameter bundles for chat TUI modules.

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
    running_started_at_ref: list
    shutting_down_ref: list
    stop_event: threading.Event
    assistant_outputs: list[str]


@dataclasses.dataclass(frozen=True)
class WorkerConfigParams:
    jobs: object
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    agent: object
    args: object
    paths: object
    use_gateway: bool
    conversation_history: list
    history_lock: threading.Lock
    build_history_context: object
    assistant_outputs: list[str]
    thinking_line_ref: list
    stream_buf_ref: list
    app_ref: list
    last_token_estimate_ref: list
    stop_event: threading.Event


@dataclasses.dataclass(frozen=True)
class StartWorkerParams:
    app_ref: list
    refresh_stop: threading.Event
    jobs: object
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    agent: object
    args: object
    paths: object
    use_gateway: bool
    conversation_history: list
    history_lock: threading.Lock
    build_history_context: object
    assistant_outputs: list[str]
    thinking_line_ref: list
    stream_buf_ref: list
    last_token_estimate_ref: list
    stop_event: threading.Event


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
    stop_event: threading.Event


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
    running_started_at_ref: list
    last_token_estimate_ref: list
    build_history_context: object
    session_manager: object
    current_session_id: str


