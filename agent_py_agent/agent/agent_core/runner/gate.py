

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...runtime_errors import runtime_error_report
from ...subagents.models import TaskStatus, task_status_in
from .timeout_policy import get_task_timeout, resolve_runner_config

if TYPE_CHECKING:
    from ...core import SimpleAgent
    from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
    from ...subagents.models import SubAgentRunnerResult, SubAgentTask


# ---------------------------------------------------------------------------
# Runner execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SingleRunnerParams:
    agent: SimpleAgent
    run_id: str
    task_timeout: float
    instruction: str
    start_runner: bool
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
    start_runners: bool
    max_cards: int
    probe: bool


@dataclass(frozen=True)
class RunnerFailureParams:
    agent: SimpleAgent
    run_id: str
    before: Any
    result: Any
    effective_instruction: str


@dataclass(frozen=True)
class _RunnerWorkerRequest:
    params: ConcurrentRunnerParams
    run_id: str
    before: Any
    retry_reason: str


def run_single_runner(params: SingleRunnerParams) -> SubAgentRunnerResult:
    from ..subagent.params import SubagentRunParams
    from .dispatch import RunSubagentWorkerParams, _run_subagent_worker

    if params.start_runner:
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
            dry_run=not params.start_runner,
            max_cards=params.max_cards,
            probe=params.probe,
            retry_reason=params.retry_reason,
        )
    )


def run_concurrent_runners(params: ConcurrentRunnerParams) -> dict[str, tuple[SubAgentRunnerResult, Any]]:
    from .dispatch import _run_subagent_worker

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


def _runner_worker_params(
    context: ConcurrentRunnerParams | None = None,
    *,
    request: _RunnerWorkerRequest | None = None,
    run_id: str = "",
    before: Any = None,
    retry_reason: str = "",
):
    from .dispatch import RunSubagentWorkerParams

    request = request or _RunnerWorkerRequest(context, run_id, before, retry_reason)
    params = request.params
    task_timeout = get_task_timeout(request.before, params.runner_timeout_seconds, params.agent.config)
    return RunSubagentWorkerParams(
        config=params.agent.config,
        root=params.agent.root,
        run_id=request.run_id,
        instruction=params.instruction,
        dry_run=not params.start_runners,
        max_cards=params.max_cards,
        probe=params.probe,
        retry_reason=request.retry_reason,
        timeout_seconds=task_timeout,
        local_store=params.agent.local_store,
        backend_override=getattr(params.agent, "_subagent_worker_backend_override", None),
    )


def _collect_runner_future_result(params: ConcurrentRunnerParams, future, run_id: str):
    try:
        return future.result()
    except Exception as exc:
        return params.agent.subagents.runner_result.record_runner_result(
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
    if not task_status_in(failure_type, {TaskStatus.BLOCKED.value, TaskStatus.TIMEOUT.value}):
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
        from ...memory_push import format_memories_for_injection, push_relevant_memories

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
    except Exception as exc:
        return _append_memory_hint(
            effective_instruction,
            _failure_memory_injection_error_hint(exc),
        )
    return effective_instruction


def _append_memory_hint(effective_instruction: str, memory_hint: str) -> str:
    if effective_instruction:
        return f"{effective_instruction}\n\n{memory_hint}"
    return memory_hint


def _failure_memory_injection_error_hint(exc: BaseException) -> str:
    report = runtime_error_report(exc, context="runner_failure.memory_injection")
    return (
        "[RUNNER_FAILURE_MEMORY_INJECTION_ERROR]\n"
        "子代理失败后的相关记忆注入失败；这不会阻止重试，但不要把它解释成没有相关历史经验。\n"
        f"{json.dumps(report, ensure_ascii=False, indent=2)}\n"
        "[/RUNNER_FAILURE_MEMORY_INJECTION_ERROR]"
    )
