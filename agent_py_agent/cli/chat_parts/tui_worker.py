# LLM: 本模块是 prompt queue 到本地/Gateway 执行路径的唯一 TUI worker；所有用户可见状态必须经 TuiRuntime typed event 发布。
# 模块用途: 串行消费聊天任务、维护运行快照、汇总回合终态并更新会话历史。

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from typing import Any

from .chat_style import CHAT_RESPONSE_STYLE_INJECT
from .history import chat_history_max_turns
from .tui_runtime import TuiRuntime, TuiTurnEventAdapter, TuiTurnSummary


# LLM: worker 与 UI 共用 queue/history/control refs；direct local_run_ref 只保存本 job 的控制句柄。
# 类用途: 汇总后台 worker 执行聊天任务需要的共享依赖。
@dataclass
class TuiWorkerConfig:
    jobs: Any
    state_lock: Any
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_request_id_ref: list
    running_started_at_ref: list
    agent: Any
    args: Any
    paths: Any
    use_gateway: bool
    conversation_history: list[tuple[str, str]]
    history_lock: Any
    build_history_context: Any
    assistant_outputs: list[str]
    last_token_estimate_ref: list
    stop_event: Any
    current_session_id: str = ""
    tui_runtime: Any | None = None
    local_run_ref: list = field(default_factory=lambda: [None])


# LLM: WorkerPathContext 把一个 job 与唯一 turn adapter 绑定；两条执行路径不得自行创建或 finalize adapter。
# 类用途: 向本地或 Gateway 路径传递回合依赖和注入内容。
@dataclass(frozen=True)
class WorkerPathContext:
    cfg: TuiWorkerConfig
    job: Any
    turn_inject: list[str]
    turn_adapter: TuiTurnEventAdapter


# LLM: ConversationTurnAppendRequest 只描述成功获得的最终对话正文；TUI display blocks 不作为历史事实源。
# 类用途: 汇总追加一次用户/助手会话历史所需参数。
@dataclass(frozen=True)
class ConversationTurnAppendRequest:
    conversation_history: list[tuple[str, str]]
    history_lock: Any
    user_message: str
    assistant_message: str
    max_turns: int


# LLM: Gateway 只发布 durable turn id；direct 在同一锁内发布消息和独立句柄，真实身份等待 core 回调。
# 函数用途: 标记聊天任务开始运行，并在 Gateway 提交完成前把精确回合 ID 保持为空。
def _tui_update_running_state(cfg: TuiWorkerConfig, job: Any) -> None:
    with cfg.state_lock:
        cfg.pending_jobs_ref[0] = max(0, int(cfg.pending_jobs_ref[0]) - 1)
        cfg.is_running_ref[0] = True
        cfg.running_prompt_ref[0] = job.user
        cfg.running_request_id_ref[0] = (
            str(getattr(job, "gateway_request_id", "") or "")
            if cfg.use_gateway
            else job.request_id
        )
        cfg.running_started_at_ref[0] = time.perf_counter()
        if not cfg.use_gateway:
            from ...agent.conversation.local_run_control import LocalRunControl

            cfg.local_run_ref[0] = LocalRunControl(job.request_id)


# LLM: cleanup 只登记最终正文并复位 worker refs；回合显示终态必须在调用它之前由 runtime 完成。
# 函数用途: 保存助手回复/会话历史、复位运行状态并确认队列任务完成。
def _tui_cleanup_after_job(
    cfg: TuiWorkerConfig,
    job: Any,
    agent_response_text: str,
    response_recorded: bool,
) -> None:
    if agent_response_text:
        if not response_recorded:
            cfg.assistant_outputs.append(agent_response_text)
        _append_conversation_turn(
            ConversationTurnAppendRequest(
                conversation_history=cfg.conversation_history,
                history_lock=cfg.history_lock,
                user_message=job.user,
                assistant_message=agent_response_text,
                max_turns=chat_history_max_turns(cfg.agent.config),
            )
        )
    _reset_worker_refs(cfg)
    cfg.jobs.task_done()


# LLM: reset 先结束旧句柄再清运行 refs；不会结算执行权或清理独立资源，不触碰下一条 job。
# 函数用途: 把 worker 快照恢复为空闲状态。
def _reset_worker_refs(cfg: TuiWorkerConfig) -> None:
    with cfg.state_lock:
        if cfg.local_run_ref[0] is not None:
            cfg.local_run_ref[0].finish()
        cfg.local_run_ref[0] = None
        cfg.is_running_ref[0] = False
        cfg.running_prompt_ref[0] = ""
        cfg.running_request_id_ref[0] = ""
        cfg.running_started_at_ref[0] = 0.0


# LLM: path selection 仅由显式 use_gateway 配置决定；两条路径返回同一 `(text, recorded, summary)` 合同。
# 函数用途: 组装本轮注入并执行本地或 Gateway 聊天路径。
def _tui_process_job(
    cfg: TuiWorkerConfig,
    job: Any,
    turn_adapter: TuiTurnEventAdapter,
) -> tuple[str, bool, TuiTurnSummary]:
    history_ctx = _worker_history_context(cfg)
    turn_inject = _build_turn_inject(
        job.inject,
        history_ctx,
        inject_complete=bool(getattr(job, "inject_complete", False)),
    )
    path_ctx = WorkerPathContext(
        cfg=cfg,
        job=job,
        turn_inject=turn_inject,
        turn_adapter=turn_adapter,
    )
    if cfg.use_gateway:
        # Gateway TUI must remain a thin client. Importing the local path here would
        # pull SimpleAgent/model/tool services into the UI process before turn 1.
        from .tui_worker_paths import _worker_gateway_path

        return _worker_gateway_path(path_ctx)
    from .tui_worker_local import _worker_local_path

    return _worker_local_path(path_ctx)


# LLM: Gateway 已从 ConversationStore 读取同一 thread 的权威历史，客户端不得再次注入内存副本；direct 路径才使用本进程历史上下文。
# 函数用途: 按明确执行路径决定本轮是否需要附加 CLI 内存历史。
def _worker_history_context(cfg: TuiWorkerConfig) -> str:
    return "" if cfg.use_gateway else str(cfg.build_history_context() or "")


# LLM: style 注入与 history context 各追加一次且次序固定；不得把 UI 文案注入模型来控制显示状态。
# 函数用途: 构造当前模型回合的附加上下文。
def _build_turn_inject(
    job_inject: list[str],
    history_ctx: str,
    *,
    inject_complete: bool = False,
) -> list[str]:
    turn_inject = list(job_inject)
    if not inject_complete:
        turn_inject.append(CHAT_RESPONSE_STYLE_INJECT)
    if history_ctx:
        turn_inject.append(history_ctx)
    return turn_inject


# LLM: 新请求先 durable submit；已有 exact Gateway job 不再次入队，但同样登记本页显示去重，不据此取得执行权。
# 函数用途: 在发布运行快照前准备真实请求编号，避免已接回的请求被通知与本地流重复显示。
def _tui_prepare_gateway_job(cfg: TuiWorkerConfig, job: Any) -> None:
    if not cfg.use_gateway:
        return
    if request_id := str(getattr(job, "gateway_request_id", "") or "").strip():
        register = getattr(getattr(cfg, "tui_runtime", None), "register_gateway_request", None)
        if callable(register):
            register(request_id)
        return
    from .tui_worker_paths import _submit_new_gateway_job

    turn_inject = _build_turn_inject(
        job.inject,
        _worker_history_context(cfg),
        inject_complete=bool(getattr(job, "inject_complete", False)),
    )
    _submit_new_gateway_job(cfg, job, turn_inject)


# LLM: 每个 dequeue 精确 begin/finalize 一次；InterruptedError 收口为中断，其它异常才记失败，不解析错误正文。
# show-prompt 在 begin 声明延迟回答，其余轮仍逐 token 显示。
# 函数用途: 后台循环串行处理聊天任务，必要时保证完整 prompt 先于最终回答显示。
def _tui_worker_body(cfg: TuiWorkerConfig) -> None:
    runtime = _required_runtime(cfg)
    while not cfg.stop_event.is_set():
        try:
            job = cfg.jobs.get(timeout=0.5)
        except queue.Empty:
            continue
        prepare_error: Exception | None = None
        try:
            _tui_prepare_gateway_job(cfg, job)
        except Exception as exc:  # noqa: BLE001 - submission failure still closes this queued turn.
            prepare_error = exc
        _tui_update_running_state(cfg, job)
        turn_adapter = runtime.begin_turn(
            job.request_id,
            task_progress_generation_id=(
                str(getattr(job, "gateway_request_id", "") or "").strip()
                or job.request_id
            ),
            defer_assistant_display=bool(getattr(job, "show_prompt", False)),
        )
        agent_response_text = ""
        response_recorded = False
        summary = TuiTurnSummary(ok=False, error="TUI worker did not produce a result")
        try:
            if prepare_error is not None:
                raise prepare_error
            agent_response_text, response_recorded, summary = _tui_process_job(
                cfg,
                job,
                turn_adapter,
            )
        except InterruptedError:
            summary = TuiTurnSummary(ok=False, interrupted=True)
        except Exception as exc:  # noqa: BLE001 worker 必须把任意执行异常收口为 typed failed turn
            summary = TuiTurnSummary(ok=False, error=str(exc))
        finally:
            runtime.complete_turn(job.request_id, summary)
            _tui_cleanup_after_job(cfg, job, agent_response_text, response_recorded)


# LLM: runtime 缺失说明组装错误，禁止新建第二 store 或回退 stdout worker。
# 函数用途: 校验 worker 使用的 TuiRuntime。
def _required_runtime(cfg: TuiWorkerConfig) -> TuiRuntime:
    if not isinstance(cfg.tui_runtime, TuiRuntime):
        raise TypeError("TuiWorkerConfig.tui_runtime must be TuiRuntime")
    return cfg.tui_runtime


# LLM: history append 在独立锁内裁剪到配置上限；UI block cache 不参与会话历史保留。
# 函数用途: 追加并有界裁剪一轮本地会话上下文。
def _append_conversation_turn(request: ConversationTurnAppendRequest) -> None:
    with request.history_lock:
        request.conversation_history.append((request.user_message, request.assistant_message))
        if len(request.conversation_history) > request.max_turns:
            request.conversation_history[:] = request.conversation_history[-request.max_turns:]


__all__ = [
    "TuiWorkerConfig",
    "WorkerPathContext",
    "_tui_prepare_gateway_job",
    "_tui_worker_body",
]
