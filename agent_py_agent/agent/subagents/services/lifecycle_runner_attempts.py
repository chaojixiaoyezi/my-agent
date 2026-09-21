# LLM: 子代理换轮在原创建锁内使用 canonical run/attempt；插话只按已确认失效身份改绑，联测恢复资格与 UNKNOWN。
# 模块用途: 协调执行轮的领取、恢复和精确放弃，保持运行账本、任务与插话身份一致。
from __future__ import annotations

"""Runner attempt lifecycle helpers for subagent runs."""

import time

from ...common.id_generator import new_id as _framework_new_id
from ...runtime_db.operations import RuntimeConflictError
from ...runtime_errors import DataCorruptionError
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


# LLM: 原创建锁覆盖 canonical 复读、准确激活和投影；文件消费与活动指针同次 mutate，先核对身份才可恢复链接与 Goal。
# 函数用途: 在同一短事务里准备新执行轮和恢复目标，避免取消或插话在换代中间插入。
def prepare_runner_attempt(
    manager: object, run_id: str, *, retry_reason: str = "",
    expected_attempt_id: str | None = None,
) -> SubAgentTask:
    with manager.creation_guard():
        task = manager.load(run_id)
        resumable_user_stop = user_stopped_run_is_resumable(task)
        _assert_runner_attempt_start_allowed(task, resumable_user_stop=resumable_user_stop)
        previous = f"{task.status}/{task.failure_type or 'none'}"
        strategy = build_subagent_recovery_strategy(
            SubagentRecoveryStrategyRequest(task=task)
        )
        attempt_id, runtime_task_id = _runtime_attempt_identity(manager, task, expected_attempt_id)
        if resumable_user_stop:
            _reactivate_user_stopped_conversation_link(manager, task)
        if not attempt_id:
            attempt_id = _framework_new_id("attempt_id")
        _recover_unsubmitted_agent_guidance(manager, task, attempt_id)
        now = time.time()

        # LLM: reducer 只更新启动所属字段，不覆盖并发授权；文件接纳在锁内复验并与 RUNNING 一起落盘。
        # 函数用途: 在最新任务上发布准确执行轮，保留准备期间产生的其它状态。
        def activate(current: SubAgentTask) -> None:
            _assert_runner_attempt_start_allowed(current, resumable_user_stop=resumable_user_stop)
            if manager.runtime_db is None:
                from ..file_runner_start import activate_file_runner_start

                activate_file_runner_start(current, expected_attempt_id, attempt_id, now)
            guidance = (task.attributes or {}).get("guidance_recovery")
            if isinstance(guidance, dict) and guidance.get("recovered_attempt_id") == attempt_id:
                current.attributes = {**dict(current.attributes or {}), "guidance_recovery": guidance}
            current.status = "RUNNING"
            current.verification_status = "UNVERIFIED"
            current.failure_type = ""
            current.ended_at = 0.0
            current.runner_active_attempt_id = attempt_id
            current.runner_last_attempt_at = current.updated_at = current.heartbeat_at = now
            _record_runner_recovery_preflight(current, strategy, previous)
            if runtime_task_id:
                _persist_runtime_task_id(current, runtime_task_id)

        task = manager.mutate(run_id, activate)
        if (
            not task_status_in(task.status, {TaskStatus.RUNNING.value})
            or task.runner_active_attempt_id != attempt_id
        ):
            raise RuntimeError(
                f"runner attempt rejected by canonical lifecycle: run_id={run_id}"
            )
        if resumable_user_stop:
            from ...conversation.goal_delegation import transition_delegated_goal

            transition_delegated_goal(manager, task, expected_status="paused", status="active")
        suffix = f" retry_reason={retry_reason}" if retry_reason else ""
        manager.actions._append_task_work_log(
            task,
            f"runner_attempt: start previous={previous} attempt={task.runner_attempts + 1} "
            f"attempt_id={attempt_id}{suffix}",
        )
        return task


# LLM: 终态资格只是接纳前提，不代替 DB 精确 pending 检查；拒绝使用冲突类型，旧 worker 不发布结果。
# 函数用途: 检查任务生命周期是否仍允许启动，保留来源工作者的原结构化门。
def _assert_runner_attempt_start_allowed(
    task: SubAgentTask,
    *,
    resumable_user_stop: bool,
) -> None:
    if (
        task_status_in(task.status, SUBAGENT_RECOVERY_CLOSED_STATUSES)
        and not resumable_user_stop
    ):
        raise RuntimeConflictError(
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
    update = getattr(getattr(store, 'tasks', None), 'update_status', None)
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


# LLM: 原创建锁与准备/控制共用；canonical mutate 只放弃显式旧 attempt，不能用旧快照清掉后来激活的 ID。
# 函数用途: 登记一个执行轮已放弃，仅当它仍是当前轮时清空指针，保留其它并发状态。
def abandon_runner_attempt(
    manager: object,
    run_id: str,
    attempt_id: str,
    *,
    reason: str = "",
) -> SubAgentTask:
    normalized = str(attempt_id or "").strip()
    with manager.creation_guard():
        if not normalized:
            return manager.load(run_id)

        # LLM: reducer 只操作最新 task 的 attempt 字段；不能在 canonical 锁里重新读取或获取 creation 锁。
        # 函数用途: 合并旧轮废弃记录，同时保护已经换代的新活动轮。
        def abandon(task: SubAgentTask) -> None:
            if normalized not in task.runner_abandoned_attempt_ids:
                task.runner_abandoned_attempt_ids.append(normalized)
            if task.runner_active_attempt_id == normalized:
                task.runner_active_attempt_id = ""
            task.updated_at = time.time()

        task = manager.mutate(run_id, abandon)
        if reason:
            manager.actions._append_task_work_log(
                task,
                f"runner_attempt: abandon attempt_id={normalized} reason={reason}",
            )
        return task


# LLM: Dead-runner supervision must reconcile the exact runtime.db attempt before
# it clears the task-file attempt and exposes the run to auto-start. A natural
# AgentRun terminal event is a monotonic closeout fact that must be projected,
# never treated as permission to rerun; recovery-generated terminal events stay
# requeueable. Keep create_attempt's duplicate/UNKNOWN fences intact.
# 函数用途: 核对子代理旧执行轮是否已安全封存；自然完成的轮次交给任务投影补写，真正崩溃的轮次才允许重新排队。
def reconcile_dead_runner_attempt(
    manager: object,
    run_id: str,
    attempt_id: str,
    *,
    reason: str = "",
) -> dict[str, object]:
    repo = getattr(manager, "runtime_db", None)
    normalized_attempt_id = str(attempt_id or "").strip()
    if repo is None or not normalized_attempt_id:
        return {"ready": True, "reason": "legacy_unmanaged"}
    run = repo.agent_run_for_run_id(str(run_id or "").strip())
    if run is None:
        return {"ready": True, "reason": "legacy_unregistered"}
    current_attempt_id = str(run["current_attempt_id"] or "").strip()
    if current_attempt_id != normalized_attempt_id:
        return {
            "ready": False,
            "reason": "runtime_attempt_mismatch",
            "current_attempt_id": current_attempt_id,
        }
    recovery_block = repo.agent_run_recovery_block_for_run_id(run_id)
    if recovery_block is not None:
        return {
            "ready": False,
            "reason": str(
                recovery_block.get("reason") or "authority_recovery_blocked"
            ),
            "recovery_block": recovery_block,
        }
    attempt = repo.get_attempt(normalized_attempt_id)
    status = str(attempt["status"] or "").strip() if attempt is not None else ""
    terminal_projection = _runtime_terminal_projection_fact(
        repo,
        normalized_attempt_id,
        run_status=str(run["status"] or "").strip(),
        attempt_status=status,
    )
    if terminal_projection is not None:
        return {
            "ready": False,
            "reason": "runtime_terminal_projection_required",
            "status": status,
            "terminal_projection": terminal_projection,
        }
    safe_statuses = {"pending", "done", "failed", "cancelled", "recovered"}
    if status in safe_statuses:
        return {"ready": True, "reason": "runtime_attempt_safe", "status": status}
    if status != "running":
        return {
            "ready": False,
            "reason": "runtime_attempt_not_reclaimable",
            "status": status,
        }
    result = repo.reclaim_orphaned_attempt(
        normalized_attempt_id,
        operator="subagent-supervision",
        reason=str(reason or "dead_runner_recovery"),
    )
    recovery_block = repo.agent_run_recovery_block_for_run_id(run_id)
    if recovery_block is not None:
        return {
            "ready": False,
            "reason": str(
                recovery_block.get("reason") or "authority_recovery_blocked"
            ),
            "recovery_block": recovery_block,
            "reclaim": result,
        }
    current = repo.agent_run_for_run_id(run_id)
    current_id = (
        str(current["current_attempt_id"] or "").strip()
        if current is not None
        else ""
    )
    if current_id != normalized_attempt_id:
        return {
            "ready": False,
            "reason": "runtime_attempt_changed_during_reclaim",
            "current_attempt_id": current_id,
            "reclaim": result,
        }
    attempt = repo.get_attempt(normalized_attempt_id)
    status = str(attempt["status"] or "").strip() if attempt is not None else ""
    ready = status in safe_statuses
    return {
        "ready": ready,
        "reason": (
            "runtime_attempt_safe"
            if ready
            else str(result.get("reason") or "runtime_attempt_busy")
        ),
        "status": status,
        "reclaim": result,
    }


# LLM: RuntimeDB terminal rows alone are not enough to close a logical child:
# explicit follow-up and source-worker slices may legitimately reopen a terminal
# AgentRun. Only the natural model-turn closeout event carries runtime_status and
# therefore proves that this stale RUNNING task projection missed its finalizer.
# 函数用途: 从当前 attempt 的追加式事件里提取“模型轮已自然结束、任务文件尚未补写”的精确恢复事实。
def _runtime_terminal_projection_fact(
    repo: object,
    attempt_id: str,
    *,
    run_status: str,
    attempt_status: str,
) -> dict[str, object] | None:
    normalized_run_status = str(run_status or "").strip().lower()
    normalized_attempt_status = str(attempt_status or "").strip().lower()
    if normalized_run_status not in {"done", "failed", "cancelled"}:
        return None
    if normalized_attempt_status != normalized_run_status:
        return None
    events_for_attempt = getattr(repo, "events_for_attempt", None)
    if not callable(events_for_attempt):
        return None
    events = list(events_for_attempt(attempt_id, limit=500) or [])
    for event in reversed(events):
        if str(event.get("event_type") or "") != "agent_run.completed":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        runtime_status = str(payload.get("runtime_status") or "").strip()
        if not runtime_status:
            # reclaim_orphaned_attempt also settles AgentRun, but that event is
            # a recovery fence rather than proof that the model turn finished.
            return None
        return {
            "event_id": str(event.get("event_id") or ""),
            "run_status": normalized_run_status,
            "attempt_status": normalized_attempt_status,
            "runtime_status": runtime_status,
            "runtime_reason": str(payload.get("runtime_reason") or ""),
            "runtime_source": str(payload.get("runtime_source") or ""),
            "turn_end_reason": str(payload.get("turn_end_reason") or ""),
            "tool_rounds": max(0, int(payload.get("tool_rounds") or 0)),
        }
    return None


# LLM: managed 激活依赖 DB 原子条件；文件预留先验证再由 canonical reducer 消费，缺记录不得降级。
# 函数用途: 领取或核对本次准确执行身份，阻止迟到启动重新打开被停止的旧轮或新轮。
def _runtime_attempt_identity(
    manager: object, task: SubAgentTask, expected_attempt_id: str | None,
) -> tuple[str, str]:
    """受管派工只激活预留轮；同步入口未预留时沿原生命周期领取。

    返回数据库 attempt 与原 task 身份。显式预留遇到缺失记录必须拒绝，
    不能降级；无数据库模式核对原文件身份，消费由后续同次任务提交完成。
    """
    repo = getattr(manager, "runtime_db", None)
    if repo is None:
        from ..file_runner_start import assert_file_runner_attempt

        if expected_attempt_id is not None:
            assert_file_runner_attempt(task, expected_attempt_id, pending_only=True)
            return expected_attempt_id, ""
        if task.runner_active_attempt_id:
            raise RuntimeConflictError("文件执行轮仍在运行，不能重复启动")
        return _framework_new_id("attempt_id"), ""
    run_id = str(task.id or "").strip()
    if not run_id:
        return "", ""
    row = repo.agent_run_for_run_id(run_id)
    if row is None:
        if expected_attempt_id is not None:
            raise RuntimeError("预留执行轮缺少原 RuntimeDB run")
        return "", ""
    attempt = repo.create_attempt(
        str(row["agent_run_id"]),
        reuse_pending=True,
        reject_running=True,
        expected_pending_attempt_id=expected_attempt_id,
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


# LLM: 插话持久操作经 guidance 领域组件； Attempt recovery may transfer only receipt states that never reached provider I/O. The
# ConversationStore owns the atomic mailbox transition; this lifecycle seam supplies exact dead
# attempt ids from RuntimeDB and persists only a bounded diagnostic projection on the task.
# 函数用途: 子代理启动恢复轮次前，安全接续尚未提交模型的用户插话并记录恢复数量。
def _recover_unsubmitted_agent_guidance(
    manager: object,
    task: SubAgentTask,
    recovered_attempt_id: str,
) -> None:
    store = getattr(manager, "conversation_store", None)
    rebind = getattr(getattr(getattr(store, 'guidance', None), 'recovery', None), 'rebind_unsubmitted', None)
    dead_attempt_ids = _rebindable_previous_attempt_ids(
        manager,
        task,
        recovered_attempt_id,
    )
    if not callable(rebind) or not dead_attempt_ids:
        return
    summary = rebind(
        "agent_run",
        str(task.id or ""),
        dead_turn_ids=dead_attempt_ids,
        recovered_turn_id=recovered_attempt_id,
    )
    if int(summary.get("errors") or 0) > 0:
        raise DataCorruptionError(
            f"subagent guidance recovery failed: run_id={task.id} "
            f"errors={summary.get('errors')}"
        )
    if not any(
        int(summary.get(key) or 0) > 0
        for key in ("rebound", "legacy_submission_unknown", "submitted_unknown")
    ):
        return
    attributes = dict(getattr(task, "attributes", {}) or {})
    attributes["guidance_recovery"] = {
        "recovered_attempt_id": recovered_attempt_id,
        "dead_attempt_ids": list(dead_attempt_ids),
        "rebound": int(summary.get("rebound") or 0),
        "legacy_submission_unknown": int(
            summary.get("legacy_submission_unknown") or 0
        ),
        "submitted_unknown": int(summary.get("submitted_unknown") or 0),
        "observed_at": time.time(),
    }
    task.attributes = attributes


# LLM: UNKNOWN/running/pending attempts are deliberately excluded: their provider or execution
# outcome is not proven dead. RuntimeDB ancestry outranks task-file abandoned projections, while
# unmanaged mode may use only its explicit abandoned-attempt ledger.
# 函数用途: 列出同一子代理中已安全结束、允许把未提交插话接到新轮次的旧 attempt。
def _rebindable_previous_attempt_ids(
    manager: object,
    task: SubAgentTask,
    recovered_attempt_id: str,
) -> tuple[str, ...]:
    recovered = str(recovered_attempt_id or "").strip()
    repo = getattr(manager, "runtime_db", None)
    if repo is None:
        return tuple(
            sorted(
                {
                    str(item or "").strip()
                    for item in task.runner_abandoned_attempt_ids
                    if str(item or "").strip() and str(item or "").strip() != recovered
                }
            )
        )
    run = repo.agent_run_for_run_id(str(task.id or "").strip())
    if run is None:
        return ()
    safe_statuses = {"done", "failed", "cancelled", "recovered"}
    return tuple(
        str(row["attempt_id"] or "").strip()
        for row in repo.attempts_for_run(str(run["agent_run_id"] or "").strip())
        if str(row["attempt_id"] or "").strip() != recovered
        and str(row["status"] or "").strip().lower() in safe_statuses
    )


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
