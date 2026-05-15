# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .runner_timeout_policy import get_task_timeout, resolve_runner_config

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..subagent import RecordRunnerResultParams, SubAgentRunnerResult, SubAgentTask


# ---------------------------------------------------------------------------
# Runner execution
# ---------------------------------------------------------------------------


# LLM: SingleRunnerParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存单个执行器参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SingleRunnerParams:
    agent: SimpleAgent
    run_id: str
    task_timeout: float
    instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool
    retry_reason: str


# LLM: ConcurrentRunnerParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存concurrent执行器参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ConcurrentRunnerParams:
    agent: SimpleAgent
    pending_jobs: list[tuple[str, Any, str]]
    runner_concurrency: int
    runner_timeout_seconds: float
    instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool


# LLM: RunnerFailureParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器失败参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerFailureParams:
    agent: SimpleAgent
    run_id: str
    before: Any
    result: Any
    effective_instruction: str


# LLM: _RunnerWorkerRequest 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器工作器请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _RunnerWorkerRequest:
    params: ConcurrentRunnerParams
    run_id: str
    before: Any
    retry_reason: str


# LLM: run_single_runner 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进单个执行器的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_single_runner(params: SingleRunnerParams) -> SubAgentRunnerResult:
    from .runner_dispatch import RunSubagentWorkerParams, _run_subagent_worker
    from .subagent_params import SubagentRunParams

    if params.execute_runners and params.task_timeout > 0:
        worker_params = RunSubagentWorkerParams(
            config=params.agent.config,
            root=params.agent.root,
            run_id=params.run_id,
            instruction=params.instruction,
            dry_run=False,
            max_cards=params.max_cards,
            probe=params.probe,
            retry_reason=params.retry_reason,
            timeout_seconds=params.task_timeout,
            local_store=params.agent.local_store,
            backend_override=getattr(params.agent, "_subagent_worker_backend_override", None),
        )
        return _run_subagent_worker(worker_params)
    return params.agent.run_subagent(
        params=SubagentRunParams(
            run_id=params.run_id,
            instruction=params.instruction,
            dry_run=not params.execute_runners,
            max_cards=params.max_cards,
            probe=params.probe,
            retry_reason=params.retry_reason,
        )
    )


# LLM: run_concurrent_runners 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进concurrentrunners的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_concurrent_runners(params: ConcurrentRunnerParams) -> dict[str, tuple[SubAgentRunnerResult, Any]]:
    from .runner_dispatch import _run_subagent_worker

    future_to_job = {}
    with ThreadPoolExecutor(max_workers=params.runner_concurrency) as executor:
        for run_id, before, retry_reason in params.pending_jobs:
            future = executor.submit(
                _run_subagent_worker,
                _runner_worker_params(request=_RunnerWorkerRequest(params, run_id, before, retry_reason)),
            )
            future_to_job[future] = (run_id, before, retry_reason)

        completed: dict[str, tuple[SubAgentRunnerResult, Any]] = {}
        for future in as_completed(future_to_job):
            run_id, before, retry_reason = future_to_job[future]
            result = _collect_runner_future_result(params, future, run_id)
            after = params.agent.subagents.load(run_id)
            completed[run_id] = (result, after)

    return completed


# LLM: _runner_worker_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器工作器参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_worker_params(
    context: ConcurrentRunnerParams | None = None,
    *,
    request: _RunnerWorkerRequest | None = None,
    run_id: str = "",
    before: Any = None,
    retry_reason: str = "",
):
    from .runner_dispatch import RunSubagentWorkerParams

    request = request or _RunnerWorkerRequest(context, run_id, before, retry_reason)
    params = request.params
    task_timeout = get_task_timeout(request.before, params.runner_timeout_seconds, params.agent.config)
    return RunSubagentWorkerParams(
        config=params.agent.config,
        root=params.agent.root,
        run_id=request.run_id,
        instruction=params.instruction,
        dry_run=not params.execute_runners,
        max_cards=params.max_cards,
        probe=params.probe,
        retry_reason=request.retry_reason,
        timeout_seconds=task_timeout,
        local_store=params.agent.local_store,
        backend_override=getattr(params.agent, "_subagent_worker_backend_override", None),
    )


# LLM: _collect_runner_future_result 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器future结果需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _collect_runner_future_result(params: ConcurrentRunnerParams, future, run_id: str):
    try:
        return future.result()
    except Exception as exc:
        return params.agent.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=run_id,
                dry_run=False,
                ok=False,
                message=f"runner worker failed: {exc}",
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type="runner_worker_error",
            )
        )


# ---------------------------------------------------------------------------
# Runner failure handling
# ---------------------------------------------------------------------------


# LLM: handle_runner_failure 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器失败的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def handle_runner_failure(params: RunnerFailureParams) -> str:
    failure_type = str(params.result.status or "").strip().upper()
    if failure_type not in {"BLOCKED", "TIMEOUT"}:
        return params.effective_instruction

    # Set pending work flag
    params.agent._has_pending_work = True

    # Inject relevant memories (push mode)
    effective_instruction = _inject_failure_memories(params, failure_type)

    # LLM introspection: analyze failure reason and suggest parameter adjustments
    params.agent._handle_failure_introspection(params.run_id, params.before, params.result)

    return effective_instruction


# LLM: _inject_failure_memories 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理inject失败memories相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _inject_failure_memories(params: RunnerFailureParams, failure_type: str) -> str:
    effective_instruction = params.effective_instruction
    try:
        from ..memory_push import format_memories_for_injection, push_relevant_memories

        task_context = {
            "task_id": params.run_id,
            "goal": getattr(params.before, "goal", ""),
            "failure_type": failure_type.lower(),
        }
        relevant_memories = push_relevant_memories(
            params.agent, failure_type.lower(), task_context, limit=3
        )
        if relevant_memories:
            memory_hint = format_memories_for_injection(relevant_memories)
            return _append_memory_hint(effective_instruction, memory_hint)
    except Exception:
        pass  # Memory injection failure doesn't affect main flow
    return effective_instruction


# LLM: _append_memory_hint 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入记忆hint的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _append_memory_hint(effective_instruction: str, memory_hint: str) -> str:
    if effective_instruction:
        return f"{effective_instruction}\n\n{memory_hint}"
    return memory_hint
