
# LLM: worker 在原状态锁内发布一次 job 的句柄，结束仅关闭原句柄；不把 request_id 当 RuntimeDB 身份。
# 模块用途: 串行执行普通终端消息，维护运行快照、控制句柄与历史。
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


# LLM: 只传递 worker 已发布的本地控制句柄；Gateway 路径仍由服务端拥有执行身份。
# 函数用途: 为本次消息组装执行依赖并选择直跑或 Gateway。
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
        local_run=cfg.local_run_ref[0],
    )
    if cfg.use_gateway:
        return _plain_gateway_handle(job_ctx)
    return _plain_local_handle(job_ctx)


# LLM: 收口保留原历史写入顺序，状态锁内关闭旧句柄后清引用；不回收后台资源。
# 函数用途: 保存普通终端回复并让输入端看到这次 worker 已结束。
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
        if cfg.local_run_ref[0] is not None:
            cfg.local_run_ref[0].finish()
        cfg.local_run_ref[0] = None
        cfg.is_running_ref[0] = False
        cfg.running_prompt_ref[0] = ""
        cfg.running_request_id_ref[0] = ""
        cfg.running_started_at_ref[0] = 0.0
    cfg.jobs.task_done()


# LLM: direct 的句柄与消息原子发布；结构化中断静默收口，不复用旧绑定或把用户中断报成执行错误。
# 函数用途: 消费消息队列，在模型执行前开放精确的本地控制入口。
def _plain_worker(cfg: PlainWorkerConfig) -> None:
    while True:
        job = cfg.jobs.get()
        with cfg.state_lock:
            cfg.pending_jobs_ref[0] -= 1
            cfg.is_running_ref[0] = True
            cfg.running_prompt_ref[0] = job.user
            cfg.running_request_id_ref[0] = job.request_id
            cfg.running_started_at_ref[0] = time.perf_counter()
            if not cfg.use_gateway:
                from ...agent.conversation.local_run_control import LocalRunControl

                cfg.local_run_ref[0] = LocalRunControl(job.request_id)
        agent_response_text = ""
        stream_started = False
        try:
            agent_response_text, stream_started = _plain_process_job(cfg, job)
        except InterruptedError:
            pass
        except Exception as exc:
            print(f"错误: {exc}")
        finally:
            _plain_finish_job(cfg, job, agent_response_text, stream_started)


# LLM: 保持输入端与 worker 的队列、状态和 local_run_ref 同一对象，不能复制可变引用。
# 函数用途: 从终端启动配置组装后台 worker 参数。
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
        running_request_id_ref=refs.running_request_id_ref,
        local_run_ref=refs.local_run_ref,
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
