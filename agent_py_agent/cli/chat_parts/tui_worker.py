# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from typing import Any

from .chat_style import CHAT_RESPONSE_STYLE_INJECT
from .renderer import GREEN, RESET
from .tui_worker_paths import _worker_gateway_path, _worker_local_path
from .tui_worker_stream import (
    _append_stream_text,
    _set_thinking_line,
)


# LLM: TuiWorkerConfig 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass
class TuiWorkerConfig:
    jobs: Any
    state_lock: Any
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    agent: Any
    args: Any
    paths: Any
    use_gateway: bool
    conversation_history: list[tuple[str, str]]
    history_lock: Any
    build_history_context: Any
    assistant_outputs: list[str]
    thinking_line_ref: list
    stream_buf_ref: list
    stream_visible_text_ref: list
    app_ref: list
    last_token_estimate_ref: list
    stop_event: Any


# LLM: WorkerPathContext 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass
class WorkerPathContext:
    cfg: TuiWorkerConfig
    job: Any
    turn_inject: list[str]
    started_at: float
    on_stream_chunk: Any
    stop_spinner: Any


# LLM: _tui_update_running_state 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护 TUI 聊天界面的输入、状态栏、退出或渲染行为。
def _tui_update_running_state(cfg: TuiWorkerConfig, job) -> None:
    with cfg.state_lock:
        cfg.pending_jobs_ref[0] -= 1
        cfg.is_running_ref[0] = True
        cfg.running_prompt_ref[0] = job.user
        cfg.running_started_at_ref[0] = time.perf_counter()


# LLM: _tui_cleanup_after_job 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护 TUI 聊天界面的输入、状态栏、退出或渲染行为。
def _tui_cleanup_after_job(
    cfg: TuiWorkerConfig,
    job,
    agent_response_text: str,
    response_recorded: bool,
) -> None:
    from .rendering import finish_tui_stream

    finish_tui_stream()
    if agent_response_text:
        if not response_recorded:
            cfg.assistant_outputs.append(agent_response_text)
        _append_conversation_turn(
            cfg.conversation_history,
            cfg.history_lock,
            job.user,
            agent_response_text,
        )
    _reset_worker_refs(cfg)
    cfg.jobs.task_done()


# LLM: _reset_worker_refs 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _reset_worker_refs(cfg: TuiWorkerConfig) -> None:
    with cfg.state_lock:
        cfg.is_running_ref[0] = False
        cfg.running_prompt_ref[0] = ""
        cfg.running_started_at_ref[0] = 0.0
    cfg.stream_buf_ref[0] = ""
    cfg.stream_visible_text_ref[0] = ""


# LLM: _tui_process_job 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护 TUI 聊天界面的输入、状态栏、退出或渲染行为。
def _tui_process_job(cfg: TuiWorkerConfig, job) -> tuple[str, bool]:
    cfg.stream_visible_text_ref[0] = ""
    history_ctx = cfg.build_history_context()
    turn_inject = _build_turn_inject(job.inject, history_ctx)
    next_message_id = len(cfg.assistant_outputs) + 1
    spinner, _on_spinner_update = _make_spinner(cfg, next_message_id)
    spinner.start()
    _begin_stream, on_stream_chunk = _make_stream_callbacks(cfg, next_message_id, spinner)
    path_ctx = WorkerPathContext(
        cfg=cfg,
        job=job,
        turn_inject=turn_inject,
        started_at=cfg.running_started_at_ref[0],
        on_stream_chunk=on_stream_chunk,
        stop_spinner=spinner.stop,
    )
    if cfg.use_gateway:
        return _worker_gateway_path(path_ctx)
    return _worker_local_path(path_ctx)


# LLM: _build_turn_inject 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _build_turn_inject(job_inject: list[str], history_ctx: str) -> list[str]:
    turn_inject = list(job_inject) + [CHAT_RESPONSE_STYLE_INJECT]
    if history_ctx:
        turn_inject.append(history_ctx)
    return turn_inject


# LLM: _tui_worker_body 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护 TUI 聊天界面的输入、状态栏、退出或渲染行为。
def _tui_worker_body(cfg: TuiWorkerConfig) -> None:
    while not cfg.stop_event.is_set():
        try:
            job = cfg.jobs.get(timeout=0.5)
        except queue.Empty:
            continue
        _tui_update_running_state(cfg, job)
        agent_response_text = ""
        response_recorded = False
        try:
            agent_response_text, response_recorded = _tui_process_job(cfg, job)
        except Exception as exc:
            _set_thinking_line("", cfg.thinking_line_ref)
            from .rendering import _cprint

            _cprint(f"错误: {exc}")
        finally:
            _tui_cleanup_after_job(cfg, job, agent_response_text, response_recorded)


# LLM: _make_spinner 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _make_spinner(cfg: TuiWorkerConfig, next_message_id: int):
    # LLM: on_spinner_update 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def on_spinner_update(text: str) -> None:
        _set_thinking_line(text, cfg.thinking_line_ref)
        if cfg.app_ref[0] is not None:
            cfg.app_ref[0].invalidate()

    from ..thinking_spinner import ThinkingSpinner

    spinner = ThinkingSpinner(
        on_update=on_spinner_update,
        on_stop=lambda: (
            _set_thinking_line("", cfg.thinking_line_ref),
            cfg.app_ref[0].invalidate() if cfg.app_ref[0] else None,
        ),
    )
    return spinner, on_spinner_update


# LLM: _make_stream_callbacks 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _make_stream_callbacks(cfg: TuiWorkerConfig, next_message_id: int, spinner):
    stream_started_ref = [False]

    # LLM: begin_stream 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def begin_stream() -> None:
        if stream_started_ref[0]:
            return
        spinner.stop()
        from .rendering import _cprint

        _cprint(f"\n{GREEN}{cfg.agent.config.agent_name}#{next_message_id}>{RESET}")
        stream_started_ref[0] = True

    # LLM: on_stream_chunk 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def on_stream_chunk(chunk: str) -> bool:
        if not chunk:
            return False
        from .renderer import strip_ansi

        if not strip_ansi(chunk).strip():
            return False
        begin_stream()
        return _append_stream_text(chunk, cfg.stream_buf_ref, cfg.stream_visible_text_ref)

    return begin_stream, on_stream_chunk


MAX_HISTORY_TURNS = 8


# LLM: _append_conversation_turn 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _append_conversation_turn(
    conversation_history: list[tuple[str, str]],
    history_lock,
    user_message: str,
    assistant_message: str,
) -> None:
    with history_lock:
        conversation_history.append((user_message, assistant_message))
        if len(conversation_history) > MAX_HISTORY_TURNS * 2:
            conversation_history[:] = conversation_history[-MAX_HISTORY_TURNS:]
