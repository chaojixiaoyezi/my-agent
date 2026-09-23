
from __future__ import annotations

# LLM: 子代理执行器复用原 attempt、权限与心跳；退出后只读同 run 最新 canonical 决定接续，旧 result 仅保留历史；联测授权收口交错、超时及准确启动。
# 模块用途: 启动和收口子代理工作片，并把新结果交回直属父级。
import logging
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ...runtime_db.operations import RuntimeConflictError
from ...settings import AgentConfig
from ...settings.services.runtime_config_task import apply_task_runtime_config_overlay
from ...subagents.authorization_gate import OperationRequest, authorize_operation
from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from ...subagents.models import FailureType, SubAgentRunnerResult
from ...subagents.runner_control import runner_attempt_cancelled
from ..subagent.params import SubagentRunParams
from .activity_diagnostics import observe_runner_activity
from .session_pool import RunnerSessionPoolLease, runner_session_lease

_LOGGER = logging.getLogger(__name__)


# LLM: expected_attempt_id 固定宿主接纳身份；None 只用于直接同步入口，后台已接纳路径必须显式传入。
# 类用途: 为一个 worker 保存不可变启动参数，等待期间不重新选取执行轮。
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
    expected_attempt_id: str | None = None


# LLM: worker 先精确激活再发布 session，退出后把原 attempt 传给待续跑核对；拒绝接纳不写 runner 结果或租约，不能覆盖后来轮次。
# 函数用途: 运行子代理工作片，续租并诊断长等待，落盘后继续同一 run 的待执行工作或唤醒直属父级。
def _run_subagent_worker(params: RunSubagentWorkerParams) -> SubAgentRunnerResult:
    from ...core import SimpleAgent

    worker = _build_worker_agent(SimpleAgent, params)
    attempt_id = ""
    launch_id = ""
    if not params.dry_run:
        try:
            prepared = worker.subagents.lifecycle.prepare_runner_attempt(
                params.run_id, retry_reason=params.retry_reason,
                expected_attempt_id=params.expected_attempt_id,
            )
        except RuntimeConflictError as exc:
            return SubAgentRunnerResult(
                run_id=params.run_id, dry_run=False, ok=False, status="CANCELLED",
                verification_status="UNVERIFIED", message=f"启动接纳已失效：{exc}",
            )
        attempt_id = prepared.runner_active_attempt_id
        start = (prepared.attributes or {}).get("background_start") or {}
        if start.get("attempt_id") == attempt_id:
            launch_id = str(start.get("launch_id") or "")
        worker._runner_activity_attempt_id = attempt_id
    try:
        if not params.dry_run:
            _record_effective_config_overlay(worker, params.run_id, prepared, attempt_id=attempt_id)
        with runner_session_lease(
            RunnerSessionPoolLease(
                manager=worker.subagents,
                run_id=params.run_id,
                worker_id=f"subagent-worker:{params.run_id}",
                interval_seconds=_runner_session_heartbeat_interval(worker),
                activity_observer=partial(observe_runner_activity, worker, params.run_id),
                attempt_id=attempt_id,
                launch_id=launch_id,
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
                result = _run_subagent_worker_interruptibly(worker, params, attempt_id=attempt_id)
            else:
                result = _run_subagent_worker_with_timeout(worker, params, attempt_id=attempt_id)
    except InterruptedError:
        raise
    except Exception as exc:
        if params.dry_run:
            raise
        # 首次租约发布失败也要沿原结果门释放已领取的执行权，不能留下活进程持有的空运行轮。
        result = worker.subagents.runner_result.record_runner_result(RecordRunnerResultParams(
            run_id=params.run_id, attempt_id=attempt_id, dry_run=False, ok=False,
            message=f"runner worker failed: {exc}", status="FAILED",
            verification_status="UNVERIFIED", failure_type=FailureType.RUNNER_WORKER_ERROR.value,
        ))
    result = _reconcile_timed_out_runner(
        worker,
        params.run_id,
        result,
        timeout_seconds=params.timeout_seconds,
    )
    _continue_pending_run_after_session(worker, params.run_id, attempt_id=attempt_id)
    _resume_direct_parent_after_session(worker, params.run_id)
    return result


# LLM: 原 result 可能早于并发授权；session 结束后复读 canonical，只为原 attempt 的 PENDING 接续。
# 不改历史结果，不复活 BLOCKED/控制终态；等待、session、RuntimeDB 与启动防重仍由原 auto-start 守门。
# 函数用途: 工作片退出后把最新待续跑状态交给原派工入口，避免已消费的授权事件留下无人接续的任务。
def _continue_pending_run_after_session(worker, run_id: str, *, attempt_id: str) -> None:
    if not attempt_id:
        return
    try:
        from ...subagents.direct_parent_lifecycle import parent_wait_blocks_dispatch

        manager = getattr(worker, "subagents", None)
        if manager is None:
            return
        task = manager.load(run_id)
        if str(task.status or "").strip().upper() != "PENDING":
            return
        session = (getattr(task, "attributes", {}) or {}).get("runner_session") or {}
        if getattr(task, "runner_active_attempt_id", "") or str(session.get("attempt_id") or "") != attempt_id:
            return
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


# LLM: 新结果只解除直属父级的等待，不等全部兄弟；原 auto-start 的 session/attempt 栅栏阻止双执行。
# 函数用途: 孩子收口后让空闲父级及时处理结果，已在工作的父级不会另开副本。
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


# LLM: 先做 owner/dispatch 授权，再恢复创建时模型引用；这里只构造依赖，激活前不得发布旧 overlay 到新执行轮。
# 函数用途: 构造子代理依赖并恢复正确模型配置，元数据在准确激活后才条件保存。
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
    return worker


def _attach_worker_runtime(worker, params: RunSubagentWorkerParams) -> None:
    if params.backend_override is not None:
        worker.backend = params.backend_override
        worker._subagent_worker_backend_override = params.backend_override
    _attach_worker_local_store(worker, params.local_store)


# LLM: 激活后在 C 内按原 attempt 窄更新；不以锁外 load/save 覆盖停止或换代后的任务。
# 函数用途: 记录本轮实际配置来源，迟到 worker 不能改写新轮元数据。
def _record_effective_config_overlay(worker, run_id: str, task, *, attempt_id: str) -> None:
    identity = getattr(task, "runtime_identity", None)
    overlay_ref = str(getattr(identity, "config_overlay_ref", "") or "").strip()
    if not overlay_ref:
        return
    overlay = {
        "schema_version": "runtime_config_overlay.v1",
        "overlay_ref": overlay_ref,
        "scope": str(getattr(identity, "config_scope", "") or "run"),
        "config_sources": getattr(worker.config, "config_sources", {}),
        "config_layers": list(getattr(worker.config, "config_layers", []) or []),
        "warnings": list(getattr(worker.config, "config_warnings", []) or []),
    }

    # LLM: 同一 canonical mutation 再核准确执行轮；异常不写入，不重建整个旧任务快照。
    # 函数用途: 只保存本次负责的配置诊断字段。
    def record(current):
        if current.runner_active_attempt_id != attempt_id or current.status != "RUNNING":
            raise RuntimeConflictError("配置元数据的原执行轮已失效")
        current.attributes = {**(current.attributes or {}), "runtime_config_overlay": overlay}

    with worker.subagents.creation_guard():
        if runner_attempt_cancelled(worker.subagents, run_id, attempt_id):
            raise RuntimeConflictError("配置元数据的原执行权已关闭")
        worker.subagents.mutate(run_id, record)


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


# LLM: 真实执行前绑定 exact attempt 给取消令牌与心跳观测；身份不可被后来的执行代替换。
# 函数用途: 给普通子代理建立可中断边界和活动身份，停止不影响共享 Gateway 内其它代理。
def _run_subagent_worker_interruptibly(
    worker,
    params: RunSubagentWorkerParams,
    *,
    attempt_id: str,
):
    from ...concurrency.interrupt import register_interruptible

    interrupt_name = f"subagent-runner-attempt:{params.run_id}:{attempt_id}"
    with register_interruptible(interrupt_name):
        if runner_attempt_cancelled(worker.subagents, params.run_id, attempt_id):
            raise InterruptedError("原子代理执行轮已停止")
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


# LLM: 仅执行显式 timeout，活动提醒不能进入这条取消路径；执行和观测使用同一个预备 attempt。
# 函数用途: 为明确有时间预算的子代理启动执行线程，到期按原合同收口，而不是因模型慢自行判死。
def _run_subagent_worker_with_timeout(worker, params: RunSubagentWorkerParams, *, attempt_id: str):
    from ...concurrency.interrupt import (
        interrupt_by_name,
        register_interruptible,
        set_interrupt,
    )

    interrupt_name = (
        f"subagent-runner-attempt:{params.run_id}:{attempt_id}"
    )
    payload: dict[str, object] = {}
    timeout_requested = threading.Event()

    def _target() -> None:
        try:
            with register_interruptible(interrupt_name):
                if runner_attempt_cancelled(worker.subagents, params.run_id, attempt_id):
                    raise InterruptedError("原子代理执行轮已停止")
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
