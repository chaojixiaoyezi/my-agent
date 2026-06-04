
from __future__ import annotations

"""Worker execution helpers for runner dispatch."""

import threading
from dataclasses import dataclass
from pathlib import Path

from ...settings import AgentConfig
from ...settings.services.runtime_config_task import apply_task_runtime_config_overlay
from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from ...subagents.models import SubAgentRunnerResult
from ..subagent.params import SubagentRunParams
from .session_pool import RunnerSessionPoolLease, runner_session_lease


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
    from ...core import SimpleAgent

    worker = _build_worker_agent(SimpleAgent, params)
    with runner_session_lease(
        RunnerSessionPoolLease(
            manager=worker.subagents,
            run_id=params.run_id,
            worker_id=f"subagent-worker:{params.run_id}",
            interval_seconds=_runner_session_heartbeat_interval(worker),
        )
    ):
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


def _build_worker_agent(simple_agent_cls, params: RunSubagentWorkerParams):
    worker = simple_agent_cls(params.config, params.root)
    _attach_worker_runtime(worker, params)
    task = worker.subagents.load(params.run_id)
    effective_config = apply_task_runtime_config_overlay(
        params.config,
        task,
        workspace_root=worker.subagents.workspace_root,
    )
    if effective_config is params.config:
        return worker
    worker = simple_agent_cls(effective_config, params.root)
    _attach_worker_runtime(worker, params)
    _record_effective_config_overlay(worker, params.run_id, task)
    return worker


def _attach_worker_runtime(worker, params: RunSubagentWorkerParams) -> None:
    if params.backend_override is not None:
        worker.backend = params.backend_override
        worker._subagent_worker_backend_override = params.backend_override
    _attach_worker_local_store(worker, params.local_store)


def _record_effective_config_overlay(worker, run_id: str, task) -> None:
    identity = getattr(task, "runtime_identity", None)
    overlay_ref = str(getattr(identity, "config_overlay_ref", "") or "").strip()
    if not overlay_ref:
        return
    refreshed = worker.subagents.load(run_id)
    attrs = dict(getattr(refreshed, "attributes", {}) or {})
    attrs["runtime_config_overlay"] = {
        "schema_version": "runtime_config_overlay.v1",
        "overlay_ref": overlay_ref,
        "scope": str(getattr(identity, "config_scope", "") or "run"),
        "config_sources": getattr(worker.config, "config_sources", {}),
        "config_layers": list(getattr(worker.config, "config_layers", []) or []),
        "warnings": list(getattr(worker.config, "config_warnings", []) or []),
    }
    refreshed.attributes = attrs
    worker.subagents.save(refreshed)


def _runner_session_heartbeat_interval(worker) -> float:
    try:
        value = float(getattr(worker.config, "background_claim_heartbeat_interval_seconds", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else 5.0


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
