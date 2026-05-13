# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

import threading
import time

from .fallback_handlers import (
    FallbackJobContext,
    _fallback_gateway_handle,
    _fallback_local_handle,
)
from .fallback_refs import FallbackInputRefs
from .fallback_state import (
    ConversationTurn,
    FallbackWorkerConfig,
    RunFallbackConfig,
    append_conversation_turn,
)
from .history import chat_history_max_turns


# LLM: _fallback_process_job 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _fallback_process_job(
    cfg: FallbackWorkerConfig,
    job,
) -> tuple[str, bool]:
    print(f"\n正在处理: {job.user}", flush=True)
    job_ctx = FallbackJobContext(
        job=job,
        agent=cfg.agent,
        args=cfg.args,
        paths=cfg.paths,
        assistant_outputs=cfg.assistant_outputs,
        build_history_context=cfg.build_history_context,
    )
    if cfg.use_gateway:
        return _fallback_gateway_handle(job_ctx)
    return _fallback_local_handle(job_ctx)


# LLM: _fallback_finish_job 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _fallback_finish_job(
    cfg: FallbackWorkerConfig,
    job,
    agent_response_text: str,
    stream_started: bool,
) -> None:
    if agent_response_text:
        if stream_started:
            cfg.assistant_outputs.append(agent_response_text)
        append_conversation_turn(
            cfg.conversation_history,
            cfg.history_lock,
            ConversationTurn(job.user, agent_response_text),
            max_turns=chat_history_max_turns(getattr(cfg.agent, "config", object())),
        )
    with cfg.state_lock:
        cfg.is_running_ref[0] = False
        cfg.running_prompt_ref[0] = ""
        cfg.running_started_at_ref[0] = 0.0
    cfg.jobs.task_done()


# LLM: _fallback_worker 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _fallback_worker(cfg: FallbackWorkerConfig) -> None:
    while True:
        job = cfg.jobs.get()
        with cfg.state_lock:
            cfg.pending_jobs_ref[0] -= 1
            cfg.is_running_ref[0] = True
            cfg.running_prompt_ref[0] = job.user
            cfg.running_started_at_ref[0] = time.perf_counter()
        agent_response_text = ""
        stream_started = False
        try:
            agent_response_text, stream_started = _fallback_process_job(cfg, job)
        except Exception as exc:
            print(f"错误: {exc}")
        finally:
            _fallback_finish_job(cfg, job, agent_response_text, stream_started)


# LLM: _make_fallback_worker_cfg 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _make_fallback_worker_cfg(
    cfg: RunFallbackConfig,
    refs: FallbackInputRefs,
) -> FallbackWorkerConfig:
    return FallbackWorkerConfig(
        jobs=cfg.jobs,
        state_lock=cfg.state_lock,
        is_running_ref=refs.is_running_ref,
        pending_jobs_ref=refs.pending_jobs_ref,
        running_prompt_ref=refs.running_prompt_ref,
        running_started_at_ref=refs.running_started_at_ref,
        agent=cfg.agent,
        args=cfg.args,
        paths=cfg.paths,
        use_gateway=cfg.use_gateway,
        assistant_outputs=refs.assistant_outputs,
        conversation_history=cfg.conversation_history,
        history_lock=cfg.history_lock,
        build_history_context=cfg.build_history_context,
    )


# LLM: _start_fallback_worker 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 维护非 TUI 聊天路径的命令处理、展示或任务提交。
def _start_fallback_worker(cfg: RunFallbackConfig, refs: FallbackInputRefs) -> None:
    worker_cfg = _make_fallback_worker_cfg(cfg, refs)
    threading.Thread(
        target=_fallback_worker,
        args=(worker_cfg,),
        daemon=True,
    ).start()


__all__ = ["_start_fallback_worker"]
