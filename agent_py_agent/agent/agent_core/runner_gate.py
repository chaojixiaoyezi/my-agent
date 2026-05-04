"""LLM: runner execution and timeout handling.

给人看的解释：
负责 runner 的并发执行、超时管理、结果收集。
不处理业务逻辑，只管"怎么跑、跑多久、结果是什么"。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..subagent import SubAgentRunnerResult, SubAgentTask


# ---------------------------------------------------------------------------
# Task timeout calculation
# ---------------------------------------------------------------------------


def get_task_timeout(
    task: SubAgentTask,
    runner_timeout_seconds: float,
    config: Any,
) -> float:
    """Calculate effective timeout for a task.

    Checks for pre-calculated dynamic timeout in task attributes first,
    then falls back to configured static timeout, and finally dynamic calculation.
    """
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
    """Resolve runner concurrency, start_rate, and timeout from config.

    Returns (runner_timeout_seconds, runner_concurrency, runner_start_rate).
    """
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


def run_single_runner(
    agent: SimpleAgent,
    run_id: str,
    task_timeout: float,
    instruction: str,
    execute_runners: bool,
    max_cards: int,
    probe: bool,
    retry_reason: str,
) -> SubAgentRunnerResult:
    """Execute a single runner with timeout."""
    from .runner_dispatch import RunSubagentWorkerParams, _run_subagent_worker

    if execute_runners and task_timeout > 0:
        params = RunSubagentWorkerParams(
            config=agent.config,
            root=agent.root,
            run_id=run_id,
            instruction=instruction,
            dry_run=False,
            max_cards=max_cards,
            probe=probe,
            retry_reason=retry_reason,
            timeout_seconds=task_timeout,
            local_store=agent.local_store,
        )
        return _run_subagent_worker(params)
    else:
        return agent.run_subagent(
            run_id,
            instruction=instruction,
            dry_run=not execute_runners,
            max_cards=max_cards,
            probe=probe,
            retry_reason=retry_reason,
        )


def run_concurrent_runners(
    agent: SimpleAgent,
    pending_jobs: list[tuple[str, Any, str]],
    runner_concurrency: int,
    runner_timeout_seconds: float,
    instruction: str,
    execute_runners: bool,
    max_cards: int,
    probe: bool,
) -> dict[str, tuple[SubAgentRunnerResult, Any]]:
    """Execute multiple runners concurrently with timeout tracking.

    Returns a dict mapping run_id to (result, after_task).
    """
    from .runner_dispatch import RunSubagentWorkerParams, _run_subagent_worker

    future_to_job = {}
    with ThreadPoolExecutor(max_workers=runner_concurrency) as executor:
        for run_id, before, retry_reason in pending_jobs:
            task_timeout = get_task_timeout(before, runner_timeout_seconds, agent.config)
            params = RunSubagentWorkerParams(
                config=agent.config,
                root=agent.root,
                run_id=run_id,
                instruction=instruction,
                dry_run=not execute_runners,
                max_cards=max_cards,
                probe=probe,
                retry_reason=retry_reason,
                timeout_seconds=task_timeout,
                local_store=agent.local_store,
            )
            future = executor.submit(_run_subagent_worker, params)
            future_to_job[future] = (run_id, before, retry_reason)

        completed: dict[str, tuple[SubAgentRunnerResult, Any]] = {}
        for future in as_completed(future_to_job):
            run_id, before, retry_reason = future_to_job[future]
            try:
                result = future.result()
            except Exception as exc:
                result = agent.subagents.record_runner_result(
                    run_id,
                    dry_run=False,
                    ok=False,
                    message=f"runner worker failed: {exc}",
                    status="BLOCKED",
                    verification_status="UNVERIFIED",
                    failure_type="runner_worker_error",
                )
            after = agent.subagents.load(run_id)
            completed[run_id] = (result, after)

    return completed


# ---------------------------------------------------------------------------
# Runner failure handling
# ---------------------------------------------------------------------------


def handle_runner_failure(
    agent: SimpleAgent,
    run_id: str,
    before: Any,
    result: Any,
    effective_instruction: str,
) -> str:
    """Handle runner failure - inject memory and trigger introspection.

    Returns the potentially modified runner instruction.
    """
    failure_type = str(result.status or "").strip().upper()
    if failure_type not in {"BLOCKED", "TIMEOUT"}:
        return effective_instruction

    # Set pending work flag
    agent._has_pending_work = True

    # Inject relevant memories (push mode)
    try:
        from ..memory_push import format_memories_for_injection, push_relevant_memories

        task_context = {
            "task_id": run_id,
            "goal": getattr(before, "goal", ""),
            "failure_type": failure_type.lower(),
        }
        relevant_memories = push_relevant_memories(
            agent, failure_type.lower(), task_context, limit=3
        )
        if relevant_memories:
            memory_hint = format_memories_for_injection(relevant_memories)
            if effective_instruction:
                effective_instruction = f"{effective_instruction}\n\n{memory_hint}"
            else:
                effective_instruction = memory_hint
    except Exception:
        pass  # Memory injection failure doesn't affect main flow

    # LLM introspection: analyze failure reason and suggest parameter adjustments
    agent._handle_failure_introspection(run_id, before, result)

    return effective_instruction
