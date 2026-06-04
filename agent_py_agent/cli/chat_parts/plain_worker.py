
from __future__ import annotations

import threading
import time

from .history import chat_history_max_turns
from .plain_handlers import (
    PlainJobContext,
    _plain_gateway_handle,
    _plain_local_handle,
)
from .plain_state import (
    ConversationTurn,
    PlainInputRefs,
    PlainWorkerConfig,
    RunPlainConfig,
    append_conversation_turn,
)


def _plain_process_job(
    cfg: PlainWorkerConfig,
    job,
) -> tuple[str, bool]:
    print(f"\n正在处理: {job.user}", flush=True)
    job_ctx = PlainJobContext(
        job=job,
        agent=cfg.agent,
        args=cfg.args,
        paths=cfg.paths,
        assistant_outputs=cfg.assistant_outputs,
        build_history_context=cfg.build_history_context,
        current_session_id=cfg.current_session_id,
    )
    if cfg.use_gateway:
        return _plain_gateway_handle(job_ctx)
    return _plain_local_handle(job_ctx)


def _plain_finish_job(
    cfg: PlainWorkerConfig,
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


def _plain_worker(cfg: PlainWorkerConfig) -> None:
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
            agent_response_text, stream_started = _plain_process_job(cfg, job)
        except Exception as exc:
            print(f"错误: {exc}")
        finally:
            _plain_finish_job(cfg, job, agent_response_text, stream_started)


def _make_plain_worker_cfg(
    cfg: RunPlainConfig,
    refs: PlainInputRefs,
) -> PlainWorkerConfig:
    return PlainWorkerConfig(
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
        current_session_id=cfg.current_session_id,
    )


def _start_plain_worker(cfg: RunPlainConfig, refs: PlainInputRefs) -> None:
    worker_cfg = _make_plain_worker_cfg(cfg, refs)
    threading.Thread(
        target=_plain_worker,
        args=(worker_cfg,),
        daemon=True,
    ).start()


__all__ = ["_start_plain_worker"]
