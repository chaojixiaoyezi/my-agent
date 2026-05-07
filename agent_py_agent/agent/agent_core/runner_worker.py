from __future__ import annotations

"""Worker execution helpers for runner dispatch."""

import threading
from dataclasses import dataclass
from pathlib import Path

from ..config import AgentConfig
from ..subagent import RecordRunnerResultParams, SubAgentRunnerResult
from .subagent_params import SubagentRunParams


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
