
from __future__ import annotations

"""Worker execution helpers for runner dispatch."""

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ...settings import AgentConfig
from ...settings.services.runtime_config_task import apply_task_runtime_config_overlay
from ...subagents.authorization_gate import OperationRequest, authorize_operation
from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from ...subagents.models import FailureType, SubAgentRunnerResult
from ..subagent.params import SubagentRunParams
from .session_pool import RunnerSessionPoolLease, runner_session_lease

_LOGGER = logging.getLogger(__name__)


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


# LLM: One worker owns one bounded model slice and then settles its runner lease.
# After settlement, continuation is driven by typed source or direct-child facts.
# 函数用途: 运行一个子代理工作片，落盘结果后继续来源岗位或唤醒直属父级。
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
        if params.dry_run:
            result = worker.run_subagent(
                params=SubagentRunParams(
                    run_id=params.run_id,
                    instruction=params.instruction,
                    dry_run=params.dry_run,
                    max_cards=params.max_cards,
                    probe=params.probe,
                    retry_reason=params.retry_reason,
                )
            )
        elif params.timeout_seconds <= 0:
            result = _run_subagent_worker_interruptibly(worker, params)
        else:
            result = _run_subagent_worker_with_timeout(worker, params)
    result = _reconcile_timed_out_runner(
        worker,
        params.run_id,
        result,
        timeout_seconds=params.timeout_seconds,
    )
    _continue_source_worker_after_session(worker, params.run_id, result)
    _resume_direct_parent_after_session(worker, params.run_id)
    return result


# LLM: The ordinary result writer runs inside runner_session_lease, so a
# PENDING source slice cannot be redispatched there: the current session still
# has a fresh "running" heartbeat and correctly vetoes double execution.  Once
# the context manager has persisted the terminal session, start the exact same
# logical run through the canonical durable auto-start path.  Do not enqueue
# this behind the owner-facing wake lane: a finding report or ordinary chat can
# occupy that lane for a full model turn while source analysis silently waits.
# Periodic orphan supervision remains the crash-safe fallback.
# 函数用途：来源工作者一轮结束后立即续派同一 run，不再等待周期性孤儿巡检。
def _continue_source_worker_after_session(worker, run_id: str, result) -> None:
    if str(getattr(result, "status", "") or "").strip().upper() != "PENDING":
        return
    try:
        from ...subagents.direct_parent_lifecycle import parent_wait_blocks_dispatch

        manager = getattr(worker, "subagents", None)
        if manager is not None:
            task = manager.load(run_id)
            if parent_wait_blocks_dispatch(task):
                # This PENDING state is an intentional event wait, not an orphan.
                return
        from ..orchestration.dispatch.capability_auto_sweep import (
            auto_start_orphan_run,
        )

        auto_start_orphan_run(worker, run_id)
    except Exception:
        # A continuation launch failure must not turn an already persisted
        # runner result into a process failure. Periodic supervision will
        # re-read the same PENDING run and retry from durable state.
        _LOGGER.warning(
            "source continuation auto-start failed (run_id=%s)",
            run_id,
            exc_info=True,
        )
        return


# LLM: A child result may wake only the canonical direct parent. Successful
# siblings are batched; failures release the parent immediately. The generic
# orphan auto-start path supplies idempotency and session-liveness fencing.
# 函数用途: 子代理工作片收口后，在等待条件满足时自动拉起它的直属父代理。
def _resume_direct_parent_after_session(worker: object, child_run_id: str) -> None:
    try:
        from ...subagents.direct_parent_lifecycle import (
            reconcile_parent_wait_for_child,
        )
        from ..orchestration.dispatch.capability_auto_sweep import (
            auto_start_orphan_run,
        )

        decision = reconcile_parent_wait_for_child(worker.subagents, child_run_id)
        if decision.should_resume:
            auto_start_orphan_run(worker, decision.parent_run_id)
    except Exception:
        # The durable wait marker remains recoverable by periodic supervision;
        # a parent wake failure must not rewrite the child's persisted result.
        _LOGGER.warning(
            "direct parent resume failed (child_run_id=%s)",
            child_run_id,
            exc_info=True,
        )


# LLM: 先做 owner/dispatch 授权，再从宿主记录恢复创建时模型引用；用户后来切模型不能改写当前 child。
# 函数用途: 构造子代理执行依赖，复用原 task overlay，并为重启后的自定义模型恢复正确接口和窗口。
def _build_worker_agent(simple_agent_cls, params: RunSubagentWorkerParams):
    worker = simple_agent_cls(params.config, params.root)
    _attach_worker_runtime(worker, params)
    # 3.txt B.4：dispatch/resume 执行阶段过统一授权查询门。系统驱动（调度器）
    # 无 requester run_id——只做 owner 一致性 + ID 形态 + 存在性；owner 不
    # 匹配 = 数据异常（任务归属另一 owner 域），fail-closed 拒绝派工。
    task = authorize_operation(
        worker.subagents,
        OperationRequest(
            operation="dispatch",
            run_id=params.run_id,
            requester_owner=str(getattr(worker.subagents, "owner_id", "") or ""),
        ),
    )
    from ...settings.model_profiles import inherited_model_config

    effective_config = apply_task_runtime_config_overlay(
        inherited_model_config(worker, task),
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


# LLM: Every real child attempt owns a named cancellation token, not only timeout-enabled
# attempts. The exact run/attempt identity is prepared before registration and passed unchanged
# into the lifecycle so an Esc/model cancel can close only this child inside a shared Gateway.
# 函数用途: 在没有超时计时器的普通子代理执行轮外包一层精确可中断边界。
def _run_subagent_worker_interruptibly(
    worker,
    params: RunSubagentWorkerParams,
):
    from ...concurrency.interrupt import register_interruptible

    prepared = worker.subagents.lifecycle.prepare_runner_attempt(
        params.run_id,
        retry_reason=params.retry_reason,
    )
    attempt_id = prepared.runner_active_attempt_id
    interrupt_name = f"subagent-runner-attempt:{params.run_id}:{attempt_id}"
    with register_interruptible(interrupt_name):
        return worker.run_subagent(
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


def _run_subagent_worker_with_timeout(worker, params: RunSubagentWorkerParams):
    from ...concurrency.interrupt import (
        interrupt_by_name,
        register_interruptible,
        set_interrupt,
    )

    prepared = worker.subagents.lifecycle.prepare_runner_attempt(
        params.run_id, retry_reason=params.retry_reason
    )
    attempt_id = prepared.runner_active_attempt_id
    interrupt_name = (
        f"subagent-runner-attempt:{params.run_id}:{attempt_id}"
    )
    payload: dict[str, object] = {}
    timeout_requested = threading.Event()

    def _target() -> None:
        try:
            with register_interruptible(interrupt_name):
                # Close the registration race: a very short timeout may fire
                # before this thread is scheduled.  In that case the target
                # marks itself interrupted before entering any model/tool work.
                if timeout_requested.is_set():
                    set_interrupt(True)
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
    try:
        task = worker.subagents.load(params.run_id)
    except (AttributeError, OSError, TypeError, ValueError):
        task = None
    if _uses_inactivity_timeout(task):
        _join_until_inactive(
            worker,
            params.run_id,
            thread,
            idle_timeout_seconds=params.timeout_seconds,
        )
    else:
        thread.join(params.timeout_seconds)
    if thread.is_alive():
        timeout_message = f"runner timed out after {params.timeout_seconds:.2f}s"
        timeout_result = worker.subagents.runner_result.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=attempt_id,
                dry_run=False,
                ok=False,
                message=timeout_message,
                status="TIMEOUT",
                verification_status="UNVERIFIED",
                failure_type=FailureType.RUNNER_TIMEOUT.value,
            )
        )
        worker.subagents.lifecycle.abandon_runner_attempt(params.run_id, attempt_id, reason=timeout_message)
        # The durable timeout/attempt fence above is authoritative.  Now ask
        # the old execution thread to close a blocking provider transport or
        # stop at its next tool-loop safe point.  If user code is truly wedged,
        # the daemon thread cannot delay process exit and its stale attempt can
        # no longer commit.
        timeout_requested.set()
        interrupt_by_name(interrupt_name)
        return timeout_result
    if "error" in payload:
        raise payload["error"]  # type: ignore[misc]
    return payload["result"]  # type: ignore[return-value]


def _uses_inactivity_timeout(task: object) -> bool:
    from ...common.audit_activation import (
        structured_audit_supervised_worker_attributes,
    )

    return structured_audit_supervised_worker_attributes(
        getattr(task, "attributes", {}) or {}
    )


def _join_until_inactive(
    worker: object,
    run_id: str,
    thread: threading.Thread,
    *,
    idle_timeout_seconds: float,
) -> None:
    """Wait for one Audit attempt, timing out only after no real activity.

    Model stream chunks and persisted successful tool progress are the two
    authoritative activity sources.  Runner-session heartbeats are omitted:
    they prove only that the host loop is alive, not that work is advancing.
    """

    idle_timeout = max(0.01, float(idle_timeout_seconds or 0.0))
    poll_seconds = min(1.0, max(0.01, idle_timeout / 10.0))
    last_activity = time.monotonic()
    previous = _runner_activity_token(worker, run_id)
    while thread.is_alive():
        thread.join(poll_seconds)
        if not thread.is_alive():
            return
        current = _runner_activity_token(worker, run_id)
        if current != previous:
            previous = current
            last_activity = time.monotonic()
            continue
        if time.monotonic() - last_activity > idle_timeout:
            # Close the tiny sample/check race: a stream chunk can arrive
            # after the snapshot above but before the timeout comparison.
            latest = _runner_activity_token(worker, run_id)
            if latest != current:
                previous = latest
                last_activity = time.monotonic()
                continue
            return


def _runner_activity_token(worker: object, run_id: str) -> tuple[object, ...]:
    ledger = getattr(worker, "_model_call_ledger", None)
    records = getattr(ledger, "records", None)
    model_activity: tuple[tuple[object, ...], ...] = ()
    if callable(records):
        try:
            model_activity = tuple(
                (
                    str(getattr(record, "call_id", "") or ""),
                    str(getattr(record, "status", "") or ""),
                    float(getattr(record, "last_activity_at", 0.0) or 0.0),
                )
                for record in records()
                if not run_id or str(getattr(record, "run_id", "") or "") == run_id
            )
        except (AttributeError, TypeError, ValueError):
            model_activity = ()
    try:
        task = worker.subagents.load(run_id)
        tool_progress = float(getattr(task, "last_progress_at", 0.0) or 0.0)
    except (AttributeError, OSError, TypeError, ValueError):
        tool_progress = 0.0
    return (model_activity, tool_progress)


def _reconcile_timed_out_runner(
    worker,
    run_id: str,
    result: SubAgentRunnerResult,
    *,
    timeout_seconds: float = 0.0,
) -> SubAgentRunnerResult:
    """Make the authoritative timeout win after the session heartbeat stops."""
    if str(result.status or "").upper() != "TIMEOUT":
        return result
    task = worker.subagents.load(run_id)
    if str(task.status or "").upper() in {"ABANDONED", "CANCELLED", "TAKEN_OVER"} or task.takeover_by:
        return result
    from ...common.audit_activation import (
        structured_audit_source_binding_attributes,
        structured_audit_source_worker_attributes,
    )

    if structured_audit_source_worker_attributes(
        getattr(task, "attributes", {}) or {}
    ):
        return _reconcile_audit_source_worker_slice(
            worker,
            task,
            result,
            timeout_seconds=timeout_seconds,
        )
    if structured_audit_source_binding_attributes(
        getattr(task, "attributes", {}) or {}
    ):
        binding_result = _reconcile_audit_source_binding_slice(
            worker,
            task,
            result,
        )
        if binding_result is not None:
            return binding_result
    now = time.time()
    task.status = "TIMEOUT"
    task.verification_status = result.verification_status or "UNVERIFIED"
    task.failure_type = FailureType.RUNNER_TIMEOUT.value
    task.result = result.message
    task.runner_attempts = max(
        int(task.runner_attempts or 0),
        int(result.runner_attempts or 0),
        len(task.runner_abandoned_attempt_ids),
    )
    task.runner_last_error = result.runner_last_error or result.message
    task.runner_active_attempt_id = ""
    task.ended_at = now
    task.updated_at = now
    task.heartbeat_at = now
    worker.subagents.save(task)
    return result


# LLM: A timed-out Audit source runner loses its attempt and ephemeral lease,
# while the durable watch, pending ACKs and stable worker task remain canonical.
# 函数用途: 来源子代理卡死或工作片到期时原地续派；窗口已结束且欠账清零则结构化完成。
def _reconcile_audit_source_worker_slice(
    worker,
    task,
    result: SubAgentRunnerResult,
    *,
    timeout_seconds: float = 0.0,
) -> SubAgentRunnerResult:
    from ...ingestion.source_worker import (
        record_source_worker_recovery,
        source_worker_task_incomplete,
    )
    from ...subagents.process_control import reclaim_background_start

    now = time.time()
    incomplete = source_worker_task_incomplete(task, now=now)
    progressed_at = float(getattr(task, "last_progress_at", 0.0) or 0.0)
    # record_runner_result() writes runner_last_attempt_at at result time, so it
    # cannot be reused as this attempt's start boundary here.  The timeout
    # window is the execution boundary already enforced by the runner.  A
    # successful tool progress event inside that window proves this was a
    # bounded slice rotation; no such event means the slice actually stalled.
    progress_boundary = now - max(0.0, float(timeout_seconds or 0.0))
    recovery_reason = (
        "slice_rotation"
        if timeout_seconds > 0 and progressed_at >= progress_boundary
        else "runner_timeout"
    )
    record_source_worker_recovery(task, reason=recovery_reason, now=now)
    task.runner_active_attempt_id = ""
    task.updated_at = now
    task.failure_type = ""
    task.runner_last_error = ""
    if incomplete:
        task.status = "PENDING"
        task.verification_status = "UNVERIFIED"
        task.ended_at = 0.0
        reclaim_background_start(task)
        result.ok = False
        result.status = "PENDING"
        result.verification_status = "UNVERIFIED"
        result.message = "Audit source worker slice expired; durable work requeued"
        log_status = "PENDING"
    else:
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        task.ended_at = now
        result.ok = True
        result.status = "DONE"
        result.verification_status = "VERIFIED"
        result.message = "Audit source window complete and backlog settled"
        log_status = "DONE"
    worker.subagents.save(task)
    worker.subagents.actions._append_task_work_log(
        task,
        f"audit_source_slice: status={log_status} reason={recovery_reason}",
    )
    return result


def _reconcile_audit_source_binding_slice(
    worker,
    task,
    result: SubAgentRunnerResult,
) -> SubAgentRunnerResult | None:
    """Retry the same pre-binding source run while its named Audit is active."""

    from ...common.audit_activation import (
        AUDIT_DEADLINE_ATTR,
    )
    from ...conversation.authority import CONVERSATION_REQUEST_ID_ATTR
    from ...ingestion.source_worker import (
        audit_parent_reconcile_state,
        record_source_worker_recovery,
    )
    from ...subagents.process_control import reclaim_background_start

    attrs = dict(getattr(task, "attributes", {}) or {})
    now = time.time()
    try:
        deadline = float(attrs.get(AUDIT_DEADLINE_ATTR) or 0.0)
    except (TypeError, ValueError):
        deadline = 0.0
    if deadline > 0 and now >= deadline:
        return None
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    active, _state = audit_parent_reconcile_state(worker, audit_id)
    if not active:
        return None
    record_source_worker_recovery(task, reason="runner_timeout", now=now)
    task.runner_active_attempt_id = ""
    task.status = "PENDING"
    task.verification_status = "UNVERIFIED"
    task.failure_type = ""
    task.runner_last_error = ""
    task.ended_at = 0.0
    task.updated_at = now
    reclaim_background_start(task)
    result.ok = False
    result.status = "PENDING"
    result.verification_status = "UNVERIFIED"
    result.message = "Audit source binding slice expired; same run requeued"
    worker.subagents.save(task)
    worker.subagents.actions._append_task_work_log(
        task,
        "audit_source_binding_slice: status=PENDING reason=runner_timeout",
    )
    return result
