
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..subagent import RecordRunnerResultParams, SubAgentRunnerResult, SubAgentTask


# ---------------------------------------------------------------------------
# Task timeout calculation
# ---------------------------------------------------------------------------


def get_task_timeout(
    task: SubAgentTask,
    runner_timeout_seconds: float,
    config: Any,
) -> float:
    from .dynamic_timeout import calculate_dynamic_timeout, estimate_task_tokens

    # Check for pre-calculated dynamic timeout
    if task.attributes and "dynamic_timeout_seconds" in task.attributes:
        timeout = float(task.attributes["dynamic_timeout_seconds"])
        if timeout > 0:
            return timeout

    # Use configured static timeout
    if runner_timeout_seconds > 0:
        return runner_timeout_seconds

    # Dynamic calculation
    estimated_input_tokens, estimated_output_tokens = estimate_task_tokens(task.goal, task.plan)
    return calculate_dynamic_timeout(
        config,
        estimated_input_tokens,
        estimated_output_tokens,
    )


def resolve_runner_config(config: Any, job_count: int) -> tuple[float, int, int]:
    from .runner_dispatch import (
        _resolve_runner_concurrency,
        _resolve_runner_start_rate,
        _resolve_runner_timeout_seconds,
    )

    runner_start_rate = _resolve_runner_start_rate(config.runner_start_rate, job_count)
    if runner_start_rate and runner_start_rate < job_count:
        job_count = runner_start_rate
    runner_concurrency = _resolve_runner_concurrency(config.runner_concurrency, job_count)
    runner_timeout_seconds = _resolve_runner_timeout_seconds(config.runner_timeout_seconds)
    return runner_timeout_seconds, runner_concurrency, runner_start_rate


# ---------------------------------------------------------------------------
# Runner execution
# ---------------------------------------------------------------------------


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


@dataclass(frozen=True)
class RunnerFailureParams:
    agent: SimpleAgent
    run_id: str
    before: Any
    result: Any
    effective_instruction: str


def run_single_runner(params: SingleRunnerParams) -> SubAgentRunnerResult:
    from .runner_dispatch import RunSubagentWorkerParams, _run_subagent_worker

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
        )
        return _run_subagent_worker(worker_params)
    return params.agent.run_subagent(
        params.run_id,
        instruction=params.instruction,
        dry_run=not params.execute_runners,
        max_cards=params.max_cards,
        probe=params.probe,
        retry_reason=params.retry_reason,
    )


def run_concurrent_runners(params: ConcurrentRunnerParams) -> dict[str, tuple[SubAgentRunnerResult, Any]]:
    from .runner_dispatch import _run_subagent_worker

    future_to_job = {}
    with ThreadPoolExecutor(max_workers=params.runner_concurrency) as executor:
        for run_id, before, retry_reason in params.pending_jobs:
            future = executor.submit(
                _run_subagent_worker,
                _runner_worker_params(params, run_id, before, retry_reason),
            )
            future_to_job[future] = (run_id, before, retry_reason)

        completed: dict[str, tuple[SubAgentRunnerResult, Any]] = {}
        for future in as_completed(future_to_job):
            run_id, before, retry_reason = future_to_job[future]
            result = _collect_runner_future_result(params, future, run_id)
            after = params.agent.subagents.load(run_id)
            completed[run_id] = (result, after)

    return completed


def _runner_worker_params(params: ConcurrentRunnerParams, run_id: str, before, retry_reason: str):
    from .runner_dispatch import RunSubagentWorkerParams

    task_timeout = get_task_timeout(before, params.runner_timeout_seconds, params.agent.config)
    return RunSubagentWorkerParams(
        config=params.agent.config,
        root=params.agent.root,
        run_id=run_id,
        instruction=params.instruction,
        dry_run=not params.execute_runners,
        max_cards=params.max_cards,
        probe=params.probe,
        retry_reason=retry_reason,
        timeout_seconds=task_timeout,
        local_store=params.agent.local_store,
    )


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


def _append_memory_hint(effective_instruction: str, memory_hint: str) -> str:
    if effective_instruction:
        return f"{effective_instruction}\n\n{memory_hint}"
    return memory_hint
