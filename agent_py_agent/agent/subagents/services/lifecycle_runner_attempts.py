from __future__ import annotations

"""Runner attempt lifecycle helpers for subagent runs."""

import time

from ...common.id_generator import new_id as _framework_new_id
from ..models import (
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    SubAgentTask,
    TaskStatus,
    task_status_in,
)
from ..recovery_eligibility import user_stopped_run_is_resumable
from .recovery.strategy import (
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)


def prepare_runner_attempt(manager: object, run_id: str, *, retry_reason: str = "") -> SubAgentTask:
    task = manager.load(run_id)
    resumable_user_stop = user_stopped_run_is_resumable(task)
    _assert_runner_attempt_start_allowed(task, resumable_user_stop=resumable_user_stop)
    if resumable_user_stop:
        _reactivate_user_stopped_conversation_link(manager, task)
    previous = f"{task.status}/{task.failure_type or 'none'}"
    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task=task)
    )
    attempt_id, runtime_task_id = _runtime_attempt_identity(manager, task)
    if not attempt_id:
        attempt_id = _framework_new_id("attempt_id")
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
    if runtime_task_id:
        _persist_runtime_task_id(task, runtime_task_id)
    manager.save(task)
    if (
        not task_status_in(task.status, {TaskStatus.RUNNING.value})
        or task.runner_active_attempt_id != attempt_id
    ):
        raise RuntimeError(
            f"runner attempt rejected by canonical lifecycle: run_id={run_id}"
        )
    suffix = f" retry_reason={retry_reason}" if retry_reason else ""
    manager.actions._append_task_work_log(
        task,
        f"runner_attempt: start previous={previous} attempt={task.runner_attempts + 1} "
        f"attempt_id={attempt_id}{suffix}",
    )
    return task


def _assert_runner_attempt_start_allowed(
    task: SubAgentTask,
    *,
    resumable_user_stop: bool,
) -> None:
    if (
        task_status_in(task.status, SUBAGENT_RECOVERY_CLOSED_STATUSES)
        and not resumable_user_stop
    ):
        raise RuntimeError(
            f"runner attempt not allowed for terminal run: run_id={task.id} "
            f"status={task.status}"
        )
    attrs = getattr(task, "attributes", {}) or {}
    from ...common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    if not structured_audit_source_worker_attributes(attrs):
        return
    try:
        from ...ingestion.source_worker import source_worker_lifecycle_state

        lifecycle = source_worker_lifecycle_state(task)
    except Exception:
        lifecycle = "unavailable"
    if lifecycle != "active":
        raise RuntimeError(
            f"runner attempt not allowed for Audit source lifecycle: "
            f"run_id={task.id} lifecycle={lifecycle}"
        )


def _reactivate_user_stopped_conversation_link(
    manager: object,
    task: SubAgentTask,
) -> None:
    """Move only the exact user-stopped child link back to active before its runner starts."""

    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    parent_task_id = str(attrs.get("conversation_task_id") or "").strip()
    if not thread_id and not parent_task_id:
        return
    if not thread_id or not parent_task_id:
        raise RuntimeError("user-stopped run has incomplete conversation identity")
    store = getattr(manager, "conversation_store", None)
    update = getattr(store, "update_task_status", None)
    if not callable(update):
        raise RuntimeError("conversation store cannot reactivate a user-stopped run")
    link = update(
        {
            "task_id": str(task.id or ""),
            "status": "active",
            "expected_status": "cancelled",
        }
    )
    if link is None or str(getattr(link, "thread_id", "") or "") != thread_id:
        raise RuntimeError("user-stopped conversation run link could not be reactivated")


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
        manager.actions._append_task_work_log(
            task,
            f"runner_attempt: abandon attempt_id={normalized} reason={reason}",
        )
    return task


def _runtime_attempt_identity(manager: object, task: SubAgentTask) -> tuple[str, str]:
    """MANAGED 下把 runner attempt 落到权威链（seq 253 闭合）。

    第一次 runner 启动原子激活创建时登记的 pending generation 1；只有前一
    次执行已经结构化收口后，重试才由 create_attempt 创建下一代。任何仍在
    running 的 current attempt 都拒绝重复启动。返回
    (DB attempt_id, 权威链 task_id)。repo 缺失或 run 未登记 → ("", "")，
    调用方回落投影 id（LOCAL_UNMANAGED / 旧 run 兼容，fail-closed 门后拦截
    与现状一致）。task_id 优先读创建时回存的 runtime_authority，缺则按
    授权门同款 JOIN 兜底查询（覆盖特性落地前已登记的老 run）。
    """
    repo = getattr(manager, "runtime_db", None)
    if repo is None:
        return "", ""
    run_id = str(task.id or "").strip()
    if not run_id:
        return "", ""
    row = repo.agent_run_for_run_id(run_id)
    if row is None:
        return "", ""
    attempt = repo.create_attempt(
        str(row["agent_run_id"]),
        reuse_pending=True,
        reject_running=True,
    )
    task_id = _runtime_authority_task_id(task) or repo.task_id_for_run_id(run_id)
    return str(attempt["attempt_id"] or ""), task_id


def _runtime_authority_task_id(task: SubAgentTask) -> str:
    attrs = getattr(task, "attributes", {})
    if not isinstance(attrs, dict):
        return ""
    authority = attrs.get("runtime_authority")
    if not isinstance(authority, dict):
        return ""
    return str(authority.get("task_id") or "").strip()


def _persist_runtime_task_id(task: SubAgentTask, task_id: str) -> None:
    """回填权威链 task_id 到任务属性（旧 run 在 prepare 时补全，run scope 读取）。"""
    attributes = dict(getattr(task, "attributes", {}) or {})
    authority = attributes.get("runtime_authority")
    authority = dict(authority) if isinstance(authority, dict) else {}
    authority["task_id"] = task_id
    attributes["runtime_authority"] = authority
    task.attributes = attributes


def _record_runner_recovery_preflight(task: SubAgentTask, strategy: object, previous_status: str) -> None:
    attributes = dict(getattr(task, "attributes", {}) or {})
    attributes["runner_recovery_preflight"] = {
        "recovery_refs": list(getattr(strategy, "recovery_refs", []) or []),
        "runner_instruction": str(getattr(strategy, "runner_instruction", "") or ""),
        "previous_status": previous_status,
        "observed_at": time.time(),
    }
    task.attributes = attributes
