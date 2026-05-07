from __future__ import annotations

"""LLM: selects runner candidates, handles retry policy, creates dispatch records, and runs worker agents.

给人看的解释：
dispatch 阶段不应该把"谁能跑、能不能重试、并发 worker 怎么启动"都塞在一个大函数里。
这个文件专门处理 runner 相关的规则和小工具。
"""

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import AgentConfig
from ..subagent import RecordRunnerResultParams, SubAgentRunnerResult, SubAgentTask
from ..subagents.services.dispatch_params import DispatchRecordParams
from .dynamic_timeout import calculate_dynamic_timeout
from .subagent_params import SubagentRunParams

if TYPE_CHECKING:
    from ..core import SimpleAgent


RETRYABLE_RUNNER_FAILURE_TYPES = {
    "runner_error",
    "structured_output_parse_error",
    "tool_result_missing",
    "model_error",
    "api_error",
    "transient_error",
    "runner_timeout",
}


def _runner_max_attempts(policy: str) -> int:

    value = str(policy or "auto").strip().lower()
    if value in {"", "auto"}:
        return 2
    if value in {"off", "none", "disabled", "false", "no"}:
        return 1
    try:
        return max(1, int(value))
    except ValueError:
        return 2


def _runner_failure_type(task: SubAgentTask) -> str:

    return str(task.failure_type or "").strip().lower()


def _runner_retry_reason(task: SubAgentTask, runner_max_attempts: int) -> str:

    if runner_max_attempts <= 1:
        return ""
    if task.status not in {"BLOCKED", "FAILED", "TIMEOUT"}:
        return ""
    failure_type = _runner_failure_type(task)
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    attempts = max(0, int(task.runner_attempts or 0))
    if attempts >= runner_max_attempts:
        return ""
    return f"failure_type={failure_type}; attempt={attempts + 1}/{runner_max_attempts}"


def _resolve_runner_concurrency(value: object, job_count: int) -> int:

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return 1
        try:
            parsed = int(normalized)
        except ValueError:
            return 1
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1
    return max(1, min(parsed, job_count))


def _resolve_runner_start_rate(value: object, job_count: int) -> int:

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return job_count
        try:
            parsed = int(normalized)
        except ValueError:
            return job_count
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return job_count
    return max(0, min(parsed, job_count))


def _resolve_runner_timeout_seconds(value: object) -> float:

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto", "off", "none", "disabled", "false", "no"}:
            return 0.0
        try:
            parsed = float(normalized)
        except ValueError:
            return 0.0
    else:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
    return max(0.0, parsed)


@dataclass(frozen=True)
class RunSubagentWorkerParams:

    config: AgentConfig
    root: Path
    run_id: str
    instruction: str
    dry_run: bool
    max_cards: int
    probe: bool
    retry_reason: str
    timeout_seconds: float = 0.0
    local_store: object | None = None
    backend_override: object | None = None


@dataclass(frozen=True)
class RunnerDispatchRecordParams:
    agent: SimpleAgent
    run_id: str
    before: SubAgentTask
    after: SubAgentTask
    result: SubAgentRunnerResult
    retry_reason: str
    execute_runners: bool


def _run_subagent_worker(params: RunSubagentWorkerParams) -> SubAgentRunnerResult:

    from ..core import SimpleAgent

    worker = SimpleAgent(params.config, params.root)
    if params.backend_override is not None:
        worker.backend = params.backend_override
    _attach_worker_local_store(worker, params.local_store)
    if params.dry_run or params.timeout_seconds <= 0:
        return worker.run_subagent(
            params=SubagentRunParams(
                run_id=params.run_id,
                instruction=params.instruction,
                dry_run=params.dry_run,
                max_cards=params.max_cards,
                probe=params.probe,
                retry_reason=params.retry_reason,
            )
        )
    return _run_subagent_worker_with_timeout(worker, params)


def _attach_worker_local_store(worker, local_store: object | None) -> None:
    if local_store is None:
        return
    worker.local_store = local_store
    worker.subagents.local_store = local_store
    worker.memory.local_store = local_store


def _run_subagent_worker_with_timeout(worker, params: RunSubagentWorkerParams):
    prepared = worker.subagents.prepare_runner_attempt(
        params.run_id, retry_reason=params.retry_reason
    )
    attempt_id = prepared.runner_active_attempt_id
    payload: dict[str, object] = {}

    def _target() -> None:
        try:
            payload["result"] = worker.run_subagent(
                params=SubagentRunParams(
                    run_id=params.run_id,
                    instruction=params.instruction,
                    dry_run=False,
                    max_cards=params.max_cards,
                    probe=params.probe,
                    retry_reason=params.retry_reason,
                    attempt_id=attempt_id,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive wrapper
            payload["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(params.timeout_seconds)
    if thread.is_alive():
        timeout_message = f"runner timed out after {params.timeout_seconds:.2f}s"
        timeout_result = worker.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=attempt_id,
                dry_run=False,
                ok=False,
                message=timeout_message,
                status="TIMEOUT",
                verification_status="UNVERIFIED",
                failure_type="runner_timeout",
            )
        )
        worker.subagents.abandon_runner_attempt(params.run_id, attempt_id, reason=timeout_message)
        return timeout_result
    if "error" in payload:
        raise payload["error"]  # type: ignore[misc]
    return payload["result"]  # type: ignore[return-value]


def _runner_dispatch_record(params: RunnerDispatchRecordParams):

    return params.agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner",
            action=(
                "retry_runner"
                if params.retry_reason and params.execute_runners
                else "execute_runner"
                if params.execute_runners
                else "runner_dry_run"
            ),
            run_id=params.run_id,
            dry_run=params.result.dry_run,
            applied=not params.result.dry_run,
            ok=params.result.ok,
            message=params.result.message,
            before_status=params.before.status,
            after_status=params.after.status,
            before_verification_status=params.before.verification_status,
            after_verification_status=params.after.verification_status,
            evidence_paths=[
                params.result.execution_context_json,
                params.result.result_json,
                params.result.output_json,
            ],
        ),
    )


def _dispatch_runner_candidates(
    tasks: list[SubAgentTask],
    max_runners: int,
    *,
    runner_max_attempts: int = 1,
) -> list[SubAgentTask]:

    if max_runners <= 0:
        return []
    candidates: list[SubAgentTask] = []
    for task in tasks:
        if not _is_dispatch_runner_candidate(task, runner_max_attempts=runner_max_attempts):
            continue
        candidates.append(task)
        if len(candidates) >= max_runners:
            break
    return candidates


def _limit_items(items: list, limit: int) -> list:

    if limit <= 0:
        return list(items)
    return list(items)[:limit]


def _is_dispatch_runner_candidate(
    task: SubAgentTask,
    *,
    runner_max_attempts: int = 1,
) -> bool:

    if task.status in {
        "AWAITING_ACCEPTANCE",
        "DONE",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }:
        return False
    if task.verification_status in {"NEEDS_ACCEPTANCE", "VERIFIED"}:
        return False
    if task.channel_status == "BROKEN":
        return False
    if any(item.status == "OPEN" for item in task.capability_requests):
        return False
    if any(item.status == "OPEN" for item in task.capability_gaps):
        return False
    if task.status == "BLOCKED":
        if _runner_failure_type(task) == "capability_request" and bool(task.capability_grants):
            return True
        return bool(_runner_retry_reason(task, runner_max_attempts))
    if task.status in {"FAILED", "TIMEOUT"}:
        return bool(_runner_retry_reason(task, runner_max_attempts))
    return task.status in {"PLANNING", "RUNNING"}


def _dispatch_patch_review_run_ids(tasks: list[SubAgentTask]) -> list[str]:

    run_ids: list[str] = []
    for task in tasks:
        if task.status != "AWAITING_ACCEPTANCE" and task.verification_status != "NEEDS_ACCEPTANCE":
            continue
        if _task_has_runner_patches(task):
            run_ids.append(task.id)
    return run_ids


def _task_has_runner_patches(task: SubAgentTask) -> bool:

    try:
        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload.get("patches"), list) and bool(payload.get("patches"))
