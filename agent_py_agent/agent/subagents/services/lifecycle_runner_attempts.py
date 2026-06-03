from __future__ import annotations

"""Runner attempt lifecycle helpers for subagent runs."""

import time

from ..models import SubAgentTask
from ..utils import _new_id
from .recovery.strategy import (
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)


def prepare_runner_attempt(manager: object, run_id: str, *, retry_reason: str = "") -> SubAgentTask:
    task = manager.load(run_id)
    previous = f"{task.status}/{task.failure_type or 'none'}"
    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(
            task=task,
            now=time.time(),
            packet_max_age_seconds=7 * 24 * 60 * 60,
        )
    )
    attempt_id = _new_id("attempt")
    now = time.time()
    task.status = "RUNNING"
    task.verification_status = "UNVERIFIED"
    task.failure_type = ""
    task.ended_at = 0.0
    task.runner_active_attempt_id = attempt_id
    task.runner_last_attempt_at = now
    task.updated_at = now
    task.heartbeat_at = now
    _record_runner_recovery_preflight(task, strategy, previous)
    manager.save(task)
    suffix = f" retry_reason={retry_reason}" if retry_reason else ""
    manager._append_task_work_log(
        task,
        f"runner_attempt: start previous={previous} attempt={task.runner_attempts + 1} "
        f"attempt_id={attempt_id}{suffix}",
    )
    return task


def abandon_runner_attempt(
    manager: object,
    run_id: str,
    attempt_id: str,
    *,
    reason: str = "",
) -> SubAgentTask:
    task = manager.load(run_id)
    normalized = str(attempt_id or "").strip()
    if not normalized:
        return task
    if normalized not in task.runner_abandoned_attempt_ids:
        task.runner_abandoned_attempt_ids.append(normalized)
    if task.runner_active_attempt_id == normalized:
        task.runner_active_attempt_id = ""
    task.updated_at = time.time()
    manager.save(task)
    if reason:
        manager._append_task_work_log(
            task,
            f"runner_attempt: abandon attempt_id={normalized} reason={reason}",
        )
    return task


def _record_runner_recovery_preflight(task: SubAgentTask, strategy: object, previous_status: str) -> None:
    if str(getattr(strategy, "packet_status", "") or "") == "ready":
        _clear_runner_recovery_preflight(task)
        return
    attributes = dict(getattr(task, "attributes", {}) or {})
    attributes["runner_recovery_preflight"] = {
        "packet_status": str(getattr(strategy, "packet_status", "") or "unknown"),
        "packet_ref": str(getattr(strategy, "packet_ref", "") or ""),
        "fallback_refs": list(getattr(strategy, "fallback_refs", []) or []),
        "runner_instruction": str(getattr(strategy, "runner_instruction", "") or ""),
        "previous_status": previous_status,
        "observed_at": time.time(),
        "save_may_regenerate_continue_packet": True,
    }
    task.attributes = attributes


def _clear_runner_recovery_preflight(task: SubAgentTask) -> None:
    attributes = dict(getattr(task, "attributes", {}) or {})
    if "runner_recovery_preflight" in attributes:
        attributes.pop("runner_recovery_preflight", None)
        task.attributes = attributes
