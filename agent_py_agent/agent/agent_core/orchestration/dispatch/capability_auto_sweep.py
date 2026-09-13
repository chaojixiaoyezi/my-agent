from __future__ import annotations

# LLM: wake 端机制层兜底(编排稳定性攻坚 §5.1,与 subagents/capability_auto_grant 成对):
#   子代理生命周期唤醒(能力申请/收尾 BLOCKED)进 LLM 整合轮【之前】,先做两件确定性工作:
#   ①信号涉及的子代理还有 OPEN 常规申请 → 机制层自动批(兜"申请端改造前遗留 / 其他
#     路径落的 OPEN 请求",判据同 capability_auto_grant,宁窄勿宽);
#   ②跑一次全量续派 dispatch(apply+start_runners)——候选完全复用既有
#     _is_dispatch_runner_candidate(BLOCKED+新 grant / PLANNING / PENDING 孤儿),
#     把"批完了却没人拉起来续跑"这最后一环也机制化,不再依赖模型调 dispatch_subagents。
#   失败绝不外抛(唤醒轮必须照常进行),结果落 debug 日志与返回摘要。
#   改动时同步检查 conversation/runtime.py(BackgroundMainAgentScheduler._run_wake_signal
#   接入点)、tests/test_capability_auto_grant.py。
#   正常工作片次数不构成存活上限；续跑与故障重试统一复用 runner candidate，UNKNOWN 仍由运行账裁决。
# 模块用途: 在唤醒和监督阶段核对能力及运行生命周期；允许长期父子协作接续，不把工作片数量当失败次数。
"""Mechanism-level capability sweep before subagent-lifecycle wake turns."""

import logging
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from ....subagents.capability_auto_grant import auto_grant_open_requests
from ....subagents.models import (
    SUBAGENT_ENDED_STATUSES,
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    task_status_in,
)
from ....subagents.services.runtime_closeout import recover_pending_closeouts
from .conversation_lifecycle_gate import conversation_lifecycle_decisions
from .params import DispatchParams
from .tool_helpers import _dispatch_capability_config

_LOGGER = logging.getLogger(__name__)

# 与 runner_completion_wake / capability_request_tool 发出的 reason 对齐。
CAPABILITY_SWEEP_REASONS = frozenset(
    {
        "subagent_capability_request_open",
        "subagent_capability_granted",
        "subagent_capability_denied",
        "subagent_runner_finished",
    }
)

def sweep_applies_to_reason(reason: object) -> bool:
    return str(reason or "").strip() in CAPABILITY_SWEEP_REASONS


# 函数用途: 唤醒轮前的机制层预处理——自动批遗留 OPEN 常规申请
#   + 全量续派停滞子代理。
#   注:可派孤儿的 durable 复活不在这条唤醒热路径上做(同步续派已覆盖同批候选),
#   由出口回收就地复活 + 调度器周期 supervision + 定时提醒 sweep 三条常驻路兜底。
def auto_capability_sweep(agent: Any, signal: Any) -> dict[str, object]:
    summary: dict[str, object] = {"auto_granted": 0, "redispatched": 0}
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return summary
    run_id = _signal_run_id(signal)
    if run_id:
        try:
            grants = auto_grant_open_requests(manager, run_id)
            summary["auto_granted"] = len(grants)
        except Exception:
            _LOGGER.warning("capability auto sweep grant failed (run_id=%s)", run_id, exc_info=True)
    try:
        if _audit_source_lifecycle_signal(signal):
            # A source slice must release the owner-facing scheduler lane.  Use
            # the same durable background start path as periodic orphan
            # recovery, rather than executing the next model slice inside this
            # wake handler.
            summary["redispatched"] = int(auto_start_orphan_run(agent, run_id).get("started") or 0)
        else:
            summary["redispatched"] = _redispatch_stalled_subagents(agent)
    except Exception:
        _LOGGER.warning("capability auto sweep redispatch failed", exc_info=True)
    return summary


def _audit_source_lifecycle_signal(signal: Any) -> bool:
    metadata = getattr(signal, "metadata", None)
    return isinstance(metadata, dict) and metadata.get("audit_source_worker") is True


# LLM: 可派孤儿的 durable 复活(§8-2 dispatch 路稳定性/§7-7 坑A坑B同治):PLANNING/PENDING
#   且没有任何活 runner 会话的 run(出口回收 requeue 的孤儿、卡在 PLANNING 的接管 run、
#   宿主进程被杀留下的僵尸),经 create_subagents 同款 auto_start(durable 后台派工,
#   scoped owner 走进程内线程/base 走独立进程)拉起——真机实锤唤醒轮上下文里同步
#   _redispatch 拉 PLANNING 不稳,auto_start 可靠。非阻塞(不占唤醒 tick 线程),
#   防重靠 background_start=launching + 候选判定的 launch_in_progress/心跳排除。
# 函数用途: 把"没人管的可派孤儿"用可靠的后台派工路一次性拉起来,返回动作摘要。
def auto_start_stalled_orphans(agent: Any) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return {"started": 0}
    try:
        tasks = manager.list_runs()
        decisions = conversation_lifecycle_decisions(agent, tasks)
        stalled: list[Any] = []
        recovery_blocks: list[dict[str, str]] = []
        for task in tasks:
            if not _is_stalled_dispatchable_orphan(
                task,
                decisions.get(str(getattr(task, "id", "") or "")),
            ):
                continue
            recovery_block = _runtime_authority_recovery_block(manager, task)
            if recovery_block is not None:
                recovery_blocks.append(recovery_block)
                continue
            stalled.append(task)
    except Exception:
        _LOGGER.warning("orphan revive list_runs failed", exc_info=True)
        return {"started": 0}
    if not stalled:
        return {
            "started": 0,
            "authority_recovery_blocked": len(recovery_blocks),
            "recovery_blocks": recovery_blocks,
        }
    from ..background.dispatch import auto_start_tasks

    result = auto_start_tasks(agent, stalled, {})
    started = (
        list(result.get("run_ids") or []) if str(result.get("status") or "") == "started" else []
    )
    return {
        "started": len(started),
        "status": str(result.get("status") or ""),
        "run_ids": started,
        "authority_recovery_blocked": len(recovery_blocks),
        "recovery_blocks": recovery_blocks,
    }


# 函数用途：在一个 runner session 已经持久化终态后，只续派指定的孤儿 run；
# 复用统一候选判定、会话判活和 durable auto-start，不扫描或带起其他任务。
def auto_start_orphan_run(agent: Any, run_id: str) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    key = str(run_id or "").strip()
    if manager is None or not key:
        return {"started": 0, "status": "unavailable", "run_ids": []}
    try:
        task = manager.load(key)
        decision = conversation_lifecycle_decisions(agent, [task]).get(key)
        if not _is_stalled_dispatchable_orphan(task, decision):
            return {"started": 0, "status": "not_needed", "run_ids": []}
        recovery_block = _runtime_authority_recovery_block(manager, task)
        if recovery_block is not None:
            return {
                "started": 0,
                "status": "authority_recovery_blocked",
                "run_ids": [],
                "recovery_block": recovery_block,
            }
    except Exception:
        _LOGGER.warning(
            "targeted orphan revive preflight failed (run_id=%s)",
            key,
            exc_info=True,
        )
        return {"started": 0, "status": "failed", "run_ids": []}
    from ..background.dispatch import auto_start_tasks

    result = auto_start_tasks(agent, [task], {})
    started = (
        list(result.get("run_ids") or []) if str(result.get("status") or "") == "started" else []
    )
    return {
        "started": len(started),
        "status": str(result.get("status") or ""),
        "run_ids": started,
    }


# LLM: Delegate eligibility and failed-run retry policy to the canonical runner candidate gate.
# Lifetime runner_attempts counts healthy child-wait/guidance slices too, never a recovery budget.
# Keep live-session, active-attempt and conversation fences; callers additionally check RuntimeDB UNKNOWN.
# 函数用途: 判断同一 run 能否安全接续；正常等孩子后的第几轮不封顶，真实失败仍走既有重试规则。
def _is_stalled_dispatchable_orphan(task: Any, decision: Any = None) -> bool:
    from ....contracts.state_machine import DISPATCHABLE_STATES, normalize_status
    from ....subagents.runner_session_liveness import has_fresh_runner_session
    from ...runner.dispatch import _is_dispatch_runner_candidate

    if normalize_status(str(getattr(task, "status", "") or "")) not in DISPATCHABLE_STATES:
        return False
    if has_fresh_runner_session(task):
        return False
    if _has_live_abandoned_runner_attempt(task):
        # A timed-out attempt is fenced from committing before its transport
        # is interrupted.  It may nevertheless need a short period to unwind.
        # Starting the replacement while that exact execution thread remains
        # registered recreates the connection/thread storm the fence was
        # designed to prevent.
        return False
    if decision is None or not decision.allowed:
        return False
    # 复用派工候选判定(launch 防重/open 能力申请/gap/verified 排除),与 dispatch 同一口径。
    return _is_dispatch_runner_candidate(task)


# LLM: This read-side preflight mirrors the runtime repository's unknown-state
# gate. It must never infer safety from task projection status, runner prose, or
# retry counts; create_attempt remains the transactional last line of defence.
# 函数用途: 在自动拉起孤儿前读取 runtime.db 权威状态，unknown 时返回结构化阻断事实。
def _runtime_authority_recovery_block(
    manager: Any,
    task: Any,
) -> dict[str, str] | None:
    repo = getattr(manager, "runtime_db", None)
    checker = getattr(repo, "agent_run_recovery_block_for_run_id", None)
    if not callable(checker):
        return None
    run_id = str(getattr(task, "id", "") or "").strip()
    if not run_id:
        return None
    return checker(run_id)


def _has_live_abandoned_runner_attempt(task: Any) -> bool:
    from ....concurrency.interrupt import is_interruptible_registered

    run_id = str(getattr(task, "id", "") or "").strip()
    if not run_id:
        return False
    for attempt_id in getattr(task, "runner_abandoned_attempt_ids", []) or []:
        selected = str(attempt_id or "").strip()
        if selected and is_interruptible_registered(f"subagent-runner-attempt:{run_id}:{selected}"):
            return True
    return False


# LLM: 周期性 supervision(worker-pool self-healing 的 reconcile 半边):事件唤醒(wake)
#   只覆盖"有人发信号"的死亡;宿主进程被 SIGKILL/断电/网关重启类静默死亡不发任何 wake,
#   靠这里周期兜底。动作=回收宿主已死的 RUNNING(requeue)→ Audit 来源岗位按持久
#   watch 补齐 →
#   durable 复活可派孤儿(刚 requeue 的同一轮就被拉起),零 LLM 成本、无候选即 no-op。
# 函数用途: 后台调度器/定时提醒路的机制层巡查:把静默死掉的岗位和孤儿捡回来。
def supervise_stalled_orphans(agent: Any) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    workspace = getattr(manager, "workspace", None)
    if not isinstance(workspace, str | Path):
        return _supervise_stalled_orphans_unlocked(agent)
    from .lock import _DispatchWatchLock

    stack = ExitStack()
    try:
        stack.enter_context(
            _DispatchWatchLock(Path(workspace) / "subagent_orphan_supervision.lock")
        )
    except RuntimeError:
        # Another trigger path is already reconciling this exact owner workspace.
        # It will persist the new canonical state before releasing the lock; the
        # next scheduled pass recomputes from disk instead of duplicating starts.
        return {
            "running_reclaimed": 0,
            "orphans_revived": 0,
            "skipped_locked": 1,
        }
    with stack:
        return _supervise_stalled_orphans_unlocked(agent)


# LLM: This is the ordered crash-recovery pass under the owner lock. Typed
# direct-parent waits are reconciled before generic orphan revival.
# 函数用途: 在锁内修复失联 runner、释放已满足的父级等待，再复活真正的孤儿。
def _supervise_stalled_orphans_unlocked(agent: Any) -> dict[str, object]:
    summary: dict[str, object] = {
        "superseded_attempts_reconciled": 0,
        "parent_closed_cancelled": 0,
        "parent_recovery_held": 0,
        "source_workers_completed": 0,
        "source_workers_completion_pending": 0,
        "running_reclaimed": 0,
        "running_terminal_projected": 0,
        "running_source_stalls_reclaimed": 0,
        "running_source_ended_reclaimed": 0,
        "stalled_source_hosts_cleanup_attempted": 0,
        "stalled_source_hosts_terminated": 0,
        "orphans_revived": 0,
        "orphan_authority_recovery_blocked": 0,
        "runtime_closeouts_recovered": 0,
        "runtime_closeouts_pending": 0,
        "runtime_closeouts_rejected": 0,
    }
    try:
        manager = getattr(agent, "subagents", None)
        repo = getattr(manager, "runtime_db", None)
        reconcile = getattr(repo, "reconcile_superseded_attempts", None)
        if callable(reconcile):
            summary["superseded_attempts_reconciled"] = len(reconcile() or [])
    except Exception:
        _LOGGER.debug("supervision stale attempt reconcile failed", exc_info=True)
    try:
        parent_summary = _reconcile_conversation_parent_lifecycle(agent)
        summary.update(parent_summary)
    except Exception:
        _LOGGER.debug("supervision parent lifecycle reconcile failed", exc_info=True)
    try:
        reclaimed = _reclaim_dead_running_runs(agent)
        summary["running_reclaimed"] = len(reclaimed)
        summary["running_terminal_projected"] = sum(
            1
            for item in reclaimed
            if item.get("recovery_action") == "terminal_projected"
        )
        summary["running_source_stalls_reclaimed"] = sum(
            1 for item in reclaimed if item.get("reason") == "runner_session_stalled"
        )
        summary["running_source_ended_reclaimed"] = sum(
            1 for item in reclaimed if item.get("reason") == "runner_session_ended"
        )
        cleanup_rows = [item for item in reclaimed if str(item.get("host_cleanup_status") or "")]
        summary["stalled_source_hosts_cleanup_attempted"] = len(
            {
                int(item.get("worker_pid") or 0)
                for item in cleanup_rows
                if int(item.get("worker_pid") or 0) > 0
            }
        )
        summary["stalled_source_hosts_terminated"] = len(
            {
                int(item.get("worker_pid") or 0)
                for item in cleanup_rows
                if str(item.get("host_cleanup_status") or "")
                in {"terminated", "killed", "not_alive"}
                and int(item.get("worker_pid") or 0) > 0
            }
        )
    except Exception:
        _LOGGER.debug("supervision running reclaim failed", exc_info=True)
    # LLM: runner 终态收口的待重试事实由这个既有关键sweep确定性推进：只补记账（settle 权威
    # run）与补通知（父级 wake），不重跑业务、不建新任务、不读任何自然语言。收口写库临时
    # 失败或"收口后通知前中断"由此收敛，而不是永久留成 task 终态 / runtime created 的矛盾。
    # 函数用途: 推进所有待重试的子代理 runner 收口事实。
    try:
        summary.update(recover_pending_closeouts(manager))
    except Exception:
        _LOGGER.debug("supervision runtime closeout recovery failed", exc_info=True)
    summary.update(_reconcile_direct_parent_waits(agent))
    try:
        # Crash/stall recovery is latency-sensitive: once a dead attempt has
        # been fenced and requeued, restart that exact durable run before the
        # broader watch-policy/source-reconciliation scans. Those scans can
        # wait on unrelated file locks and previously delayed an already
        # recovered source worker by another full minute.
        _merge_orphan_revive_summary(summary, auto_start_stalled_orphans(agent))
    except Exception:
        _LOGGER.debug("supervision immediate orphan revive failed", exc_info=True)
    try:
        # Reuse this canonical periodic reconciler: one persisted Audit watch
        # gets one idempotent source worker. No second scheduler or Agent loop.
        from ....ingestion.source_worker import reconcile_audit_source_workers

        source_summary = reconcile_audit_source_workers(agent)
        summary["audit_source_workers"] = source_summary
    except Exception:
        _LOGGER.debug("supervision Audit source worker reconcile failed", exc_info=True)
    else:
        # Reconciliation may materialize a genuinely missing source position.
        # Only that structural creation needs a second pass; ordinary sweeps
        # keep a single auto-start call.
        workers = source_summary.get("workers") if isinstance(source_summary, dict) else []
        if any(
            isinstance(item, dict) and item.get("created") is True
            for item in (workers if isinstance(workers, list) else [])
        ):
            try:
                summary["orphans_revived"] += int(
                    auto_start_stalled_orphans(agent).get("started") or 0
                )
            except Exception:
                _LOGGER.debug(
                    "supervision reconciled source revive failed",
                    exc_info=True,
                )
    return summary


# LLM: Supervisor reporting must distinguish accepted starts from authority
# blocks without duplicating the orphan-start policy or changing task state.
# 函数用途: 把一次孤儿恢复扫描的结构化结果合并到 Gateway 监督摘要。
def _merge_orphan_revive_summary(
    summary: dict[str, object],
    revive_summary: dict[str, object],
) -> None:
    summary["orphans_revived"] = int(revive_summary.get("started") or 0)
    summary["orphan_authority_recovery_blocked"] = int(
        revive_summary.get("authority_recovery_blocked") or 0
    )
    if revive_summary.get("recovery_blocks"):
        summary["orphan_recovery_blocks"] = list(
            revive_summary.get("recovery_blocks") or []
        )


# LLM: 一路等待巡检失败不停止其他监督，但必须留下独立失败计数而非零检查假健康。
# 函数用途: 调和直属等待账，把成功扫描与读取失败分开计数并记录告警。
def _reconcile_direct_parent_waits(agent: Any) -> dict[str, int]:
    try:
        from ....subagents.direct_parent_lifecycle import reconcile_all_parent_waits

        wait_summary = reconcile_all_parent_waits(getattr(agent, "subagents", None))
        if wait_summary.get("ok") is False:
            _LOGGER.warning("direct parent wait unavailable: %s", wait_summary.get("error_type"))
        return {
            "direct_parent_waits_checked": int(wait_summary.get("checked") or 0),
            "direct_parent_waits_released": int(wait_summary.get("released") or 0),
            "direct_parent_waits_errors": int(wait_summary.get("ok") is False),
        }
    except Exception:
        _LOGGER.warning("supervision direct parent wait reconcile failed", exc_info=True)
        return {
            "direct_parent_waits_checked": 0,
            "direct_parent_waits_released": 0,
            "direct_parent_waits_errors": 1,
        }


# LLM: 宿主已死的 RUNNING 回收(重启/SIGKILL 韧性的最后一环):RUNNING 但 runner 会话
#   心跳过期【且】会话宿主 pid 已死 → runner 线程必已消亡,abandon 当前 attempt 并
#   requeue PENDING(同一轮 supervision 的复活扫描随即拉起续跑)。判据全结构化且双重
#   保守:心跳新鲜不动;宿主 pid 还活着也不动(可能只是心跳抖动/长 GC,交给出口回收
#   的活性豁免链处置);无会话事实的老数据不动。
# 函数用途: 网关重启/进程被杀后,把"看着在跑其实早死了"的 run 放回队列续命。
def _reclaim_dead_running_runs(agent: Any) -> list[dict[str, object]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return []
    reclaimed: list[dict[str, object]] = []
    tasks = manager.list_runs()
    fully_stalled_host_pids = _fully_stalled_source_host_pids(tasks)
    # A suspended host can retain task/watch file locks indefinitely.  Fence
    # the host process before rewriting its durable attempts; doing this in
    # the opposite order made five frozen workers consume several lock
    # timeouts apiece before the process was finally killed.
    stalled_host_cleanup = _terminate_fully_stalled_source_hosts(fully_stalled_host_pids)
    decisions = conversation_lifecycle_decisions(agent, tasks)
    for task in tasks:
        decision = decisions.get(str(getattr(task, "id", "") or ""))
        facts = _dead_running_reclaim_facts(
            task,
            decision,
            fully_stalled_host_pids,
            stalled_host_cleanup,
        )
        if facts is None:
            continue
        run_id = str(facts["run_id"])
        reason = str(facts["reason"])
        try:
            if _requeue_dead_running(manager, task, run_id, reason=reason):
                repaired = manager.load(run_id)
                repaired_attrs = getattr(repaired, "attributes", {}) or {}
                projection_repair = (
                    repaired_attrs.get("runtime_terminal_projection_repair")
                    if isinstance(repaired_attrs, dict)
                    else None
                )
                facts["recovery_action"] = (
                    "terminal_projected"
                    if isinstance(projection_repair, dict)
                    and str(projection_repair.get("attempt_id") or "")
                    == str(getattr(task, "runner_active_attempt_id", "") or "")
                    else "requeued"
                )
                reclaimed.append(facts)
        except Exception:
            _LOGGER.warning("supervision reclaim failed (run_id=%s)", run_id, exc_info=True)
    return reclaimed


def _dead_running_reclaim_facts(
    task: Any,
    decision: Any,
    fully_stalled_host_pids: set[int],
    stalled_host_cleanup: dict[int, dict[str, object]],
) -> dict[str, object] | None:
    from ....common.audit_activation import (
        structured_audit_source_worker_attributes,
        structured_audit_supervised_worker_attributes,
    )
    from ....subagents.runner_session_liveness import (
        has_fresh_runner_session,
        runner_session_of,
    )

    if str(getattr(task, "status", "") or "").strip().upper() != "RUNNING":
        return None
    attrs = getattr(task, "attributes", {}) or {}
    supervised = structured_audit_supervised_worker_attributes(attrs)
    if not _reclaim_decision_allows(decision, supervised):
        return None
    session = runner_session_of(task)
    if not session:
        return None
    worker_pid, worker_alive, cross_gen, proven_dead_despite_fresh_heartbeat = (
        _runner_process_reclaim_facts(session)
    )
    # 持久心跳用于容忍慢模型、GC 和磁盘抖动，但已经退出的 OS 进程是更强的客观事实。
    # Gateway 重启后旧 in-process runner 的最后一次心跳可能仍在 45 秒新鲜窗内；若
    # exact PID 已死，就立即回收。PID 仍活（含复用）时继续等心跳过期，避免双执行。
    if has_fresh_runner_session(task) and not proven_dead_despite_fresh_heartbeat:
        return None
    ended = _ended_source_worker_with_work(
        task,
        session,
        structured_audit_source_worker_attributes(attrs),
    )
    if str(session.get("status") or "").strip() not in {"starting", "running"} and not ended:
        return None
    worker_pid = _session_worker_pid(session)
    fully_stalled = worker_pid in fully_stalled_host_pids
    cleanup = stalled_host_cleanup.get(worker_pid, {})
    if fully_stalled and str(cleanup.get("status") or "") not in {
        "terminated",
        "killed",
        "not_alive",
    }:
        return None
    if not cross_gen and not supervised and (worker_pid <= 0 or worker_alive):
        return None
    stalled = supervised and (fully_stalled or worker_pid <= 0 or worker_alive)
    facts: dict[str, object] = {
        "run_id": str(getattr(task, "id", "") or ""),
        "reason": "runner_session_ended"
        if ended
        else ("runner_session_stalled" if stalled else "runner_process_died"),
        "worker_pid": worker_pid,
    }
    if fully_stalled:
        facts.update(
            host_cleanup_status=str(cleanup.get("status") or ""),
            host_cleanup_escalated=bool(cleanup.get("escalated")),
        )
    return facts


# LLM: OS process death may override only an explicitly shaped runner session; legacy rows with
# no process kind stay on heartbeat freshness to avoid an unsafe eager reclaim.
# 函数用途: 提取 runner PID、存活、跨进程代次与“新鲜心跳也可安全回收”四个客观事实。
def _runner_process_reclaim_facts(
    session: dict[str, object],
) -> tuple[int, bool, bool, bool]:
    from ....subagents.process_control import PROCESS_EPOCH, is_pid_alive

    worker_pid = _session_worker_pid(session)
    worker_alive = worker_pid > 0 and is_pid_alive(worker_pid)
    session_epoch = str(session.get("process_epoch") or "")
    in_process = session.get("in_process")
    cross_gen = in_process is True and bool(session_epoch) and session_epoch != PROCESS_EPOCH
    proven_dead = worker_pid > 0 and not worker_alive and (
        in_process is False or (in_process is True and cross_gen)
    )
    return worker_pid, worker_alive, cross_gen, proven_dead


def _reclaim_decision_allows(decision: Any, supervised: bool) -> bool:
    return bool(
        decision is not None
        and (
            decision.allowed
            or decision.should_complete
            or (
                supervised
                and str(getattr(decision, "reason", "") or "") == "audit_source_waiting_for_records"
            )
        )
    )


def _ended_source_worker_with_work(task: Any, session: dict[str, object], source: bool) -> bool:
    if not source or str(session.get("status") or "").strip() not in {"completed", "failed"}:
        return False
    from ....ingestion.source_worker import source_worker_lifecycle_state

    return source_worker_lifecycle_state(task) == "active"


def _session_worker_pid(session: dict[str, object]) -> int:
    try:
        return int(session.get("worker_pid") or 0)
    except (TypeError, ValueError):
        return 0


# LLM: A suspended independent dispatch host can retain memory and file
# descriptors forever even after every durable source attempt on it has been
# fenced and requeued.  Reuse the canonical process-group escalation primitive,
# but only when every still-running session on the PID is an Audit source
# worker and every one has stale heartbeat.  A healthy sibling or an in-process
# gateway runner vetoes host termination.
# 函数用途: 找出“整个独立来源工作者宿主都已假死”的 PID；不靠进程名或 Prompt 判断。
def _fully_stalled_source_host_pids(tasks: list[object]) -> set[int]:
    import os

    from ....common.audit_activation import (
        structured_audit_supervised_worker_attributes,
    )
    from ....subagents.process_control import is_pid_alive
    from ....subagents.runner_session_liveness import (
        has_fresh_runner_session,
        runner_session_of,
    )

    sessions_by_pid: dict[int, list[tuple[object, dict[str, object]]]] = {}
    for task in tasks:
        session = runner_session_of(task)
        if str(session.get("status") or "").strip() not in {"starting", "running"}:
            continue
        try:
            pid = int(session.get("worker_pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        sessions_by_pid.setdefault(pid, []).append((task, session))

    stalled: set[int] = set()
    for pid, rows in sessions_by_pid.items():
        if pid == os.getpid() or not is_pid_alive(pid):
            continue
        if any(bool(session.get("in_process")) for _task, session in rows):
            continue
        if any(
            not structured_audit_supervised_worker_attributes(getattr(task, "attributes", {}) or {})
            for task, _session in rows
        ):
            continue
        if any(
            str(getattr(task, "status", "") or "").strip().upper() != "RUNNING"
            for task, _session in rows
        ):
            continue
        if any(has_fresh_runner_session(task) for task, _session in rows):
            continue
        stalled.add(pid)
    return stalled


# 函数用途: 回收已由上面全宿主判据确认的假死进程；只有终止事实明确后才允许重派。
def _terminate_fully_stalled_source_hosts(
    host_pids: set[int],
) -> dict[int, dict[str, object]]:
    if not host_pids:
        return {}
    from ....subagents.process_control import terminate_pid_with_escalation

    return {pid: dict(terminate_pid_with_escalation(pid) or {}) for pid in sorted(host_pids)}


# LLM: Task-file and runtime.db attempts are two projections of one execution.
# A natural runtime terminal event must run the ordinary structured finalizer
# instead of reopening work; only a fenced crash attempt becomes PENDING.
# 函数用途: 回收已确认失联的 runner；自然终态补齐任务结果，真正崩溃才改成待派状态。
def _requeue_dead_running(
    manager: Any,
    task: Any,
    run_id: str,
    *,
    reason: str,
) -> bool:
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if attempt_id:
        authority = manager.lifecycle.reconcile_dead_runner_attempt(
            run_id,
            attempt_id,
            reason=f"supervision_{reason}_reclaim",
        )
        terminal_projection = authority.get("terminal_projection")
        if isinstance(terminal_projection, dict):
            return _project_runtime_terminal_result(
                manager,
                run_id,
                attempt_id,
                terminal_projection,
                reason=reason,
            )
        if not bool(authority.get("ready")):
            _LOGGER.info(
                "supervision requeue deferred (run_id=%s attempt_id=%s reason=%s)",
                run_id,
                attempt_id,
                str(authority.get("reason") or "runtime_attempt_not_ready"),
            )
            return False
        manager.lifecycle.abandon_runner_attempt(
            run_id,
            attempt_id,
            reason=f"supervision_{reason}_reclaim",
        )
    refreshed = manager.load(run_id)
    from ....ingestion.source_worker import record_source_worker_recovery

    record_source_worker_recovery(refreshed, reason=reason)
    _close_reclaimed_runner_session(refreshed, reason=reason)
    refreshed.status = "PENDING"
    refreshed.failure_type = ""
    from ....subagents.process_control import reclaim_background_start

    reclaim_background_start(refreshed)
    manager.save(refreshed)
    manager.actions._append_task_work_log(
        refreshed,
        f"supervision: requeued RUNNING->PENDING reason={reason}",
    )
    return True


# LLM: This crash repair deliberately reuses the canonical runner-result service,
# including capability/source-worker overrides and parent wake delivery. Runtime
# event fields own lifecycle; no model prose, artifact, or test claim is parsed.
# 函数用途: Gateway 在“运行账本已收口、任务文件还没来得及收口”时补写同一轮结果，避免把已完成子代理再跑一遍。
def _project_runtime_terminal_result(
    manager: Any,
    run_id: str,
    attempt_id: str,
    terminal_projection: dict[str, object],
    *,
    reason: str,
) -> bool:
    from ....subagents.manager_runner_result_payload import RecordRunnerResultParams
    from ....turn_end import infer_turn_end_reason, subagent_outcome_for_turn_end

    runtime_status = str(terminal_projection.get("runtime_status") or "").strip()
    runtime_reason = str(terminal_projection.get("runtime_reason") or "").strip()
    turn_end_reason = infer_turn_end_reason(
        explicit=terminal_projection.get("turn_end_reason"),
        runtime_status=runtime_status,
        runtime_reason=runtime_reason,
    )
    status, failure_type, ok = subagent_outcome_for_turn_end(turn_end_reason)
    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=ok,
            message=(
                "Gateway 恢复时发现本轮运行账本已收口；"
                "已按结构化终态补齐子代理任务投影。"
            ),
            status=status,
            turn_end_reason=turn_end_reason,
            failure_type=failure_type,
            tool_rounds=max(
                0,
                int(terminal_projection.get("tool_rounds") or 0),
            ),
        )
    )
    repaired = manager.load(run_id)
    if (
        str(getattr(repaired, "status", "") or "").strip().upper() == "RUNNING"
        and str(getattr(repaired, "runner_active_attempt_id", "") or "").strip()
        == attempt_id
    ):
        _LOGGER.warning(
            "runtime terminal projection did not settle task (run_id=%s attempt_id=%s)",
            run_id,
            attempt_id,
        )
        return False
    _close_runtime_terminal_runner_session(
        repaired,
        status=str(getattr(result, "status", "") or repaired.status),
        reason=reason,
    )
    attributes = dict(getattr(repaired, "attributes", {}) or {})
    attributes["runtime_terminal_projection_repair"] = {
        "schema_version": "runtime-terminal-projection-repair.v1",
        "attempt_id": attempt_id,
        "event_id": str(terminal_projection.get("event_id") or ""),
        "run_status": str(terminal_projection.get("run_status") or ""),
        "task_status": str(getattr(repaired, "status", "") or ""),
        "turn_end_reason": turn_end_reason,
        "observed_at": time.time(),
    }
    repaired.attributes = attributes
    manager.save(repaired)
    manager.actions._append_task_work_log(
        repaired,
        "supervision: projected runtime terminal attempt without rerun "
        f"attempt_id={attempt_id} status={repaired.status}",
    )
    return True


# LLM: The dead process cannot emit its runner-session epilogue after restart;
# close only the exact current receipt after the canonical result projection has
# succeeded. This is display/liveness evidence, never task-completion authority.
# 函数用途: 给已由运行账本和结果服务确认收口的旧 runner 会话补上结束时间。
def _close_runtime_terminal_runner_session(
    task: Any,
    *,
    status: str,
    reason: str,
) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    session = attrs.get("runner_session")
    if not isinstance(session, dict):
        return
    if str(session.get("status") or "").strip().lower() not in {
        "starting",
        "running",
    }:
        return
    now = time.time()
    task_status = str(status or "").strip().upper()
    session_status = (
        "completed"
        if task_status == "DONE"
        else "cancelled"
        if task_status in {"CANCELLED", "ABANDONED", "TAKEN_OVER"}
        else "failed"
    )
    closed = dict(session)
    closed.update(
        status=session_status,
        heartbeat_at=now,
        ended_at=now,
        reconcile_reason=str(reason or "runtime_terminal_projection"),
    )
    attrs["runner_session"] = closed
    task.attributes = attrs


# LLM: Once runtime.db has fenced the exact dead attempt, its durable liveness receipt must
# become terminal in the same canonical save as RUNNING->PENDING. Otherwise the immediate
# orphan-start pass sees the old fresh heartbeat and delays a proven-safe restart for 45 seconds.
# 函数用途: 把已确认死亡且已封存的 runner 会话标成失败终态，让同轮监督可以立即续派。
def _close_reclaimed_runner_session(task: Any, *, reason: str) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    session = attrs.get("runner_session")
    if not isinstance(session, dict):
        return
    if str(session.get("status") or "").strip().lower() not in {"starting", "running"}:
        return
    now = time.time()
    closed = dict(session)
    closed.update(
        status="failed",
        heartbeat_at=now,
        ended_at=now,
        reclaim_reason=str(reason or "runner_reclaimed"),
    )
    attrs["runner_session"] = closed
    task.attributes = attrs


def _reconcile_conversation_parent_lifecycle(agent: Any) -> dict[str, int]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return {
            "parent_closed_cancelled": 0,
            "parent_recovery_held": 0,
            "source_workers_completed": 0,
            "source_workers_completion_pending": 0,
        }
    tasks = manager.list_runs()
    decisions = conversation_lifecycle_decisions(agent, tasks)
    cancelled = 0
    completed = 0
    completion_pending = 0
    held = 0
    for task in tasks:
        decision = decisions.get(str(getattr(task, "id", "") or ""))
        if decision is None or decision.allowed:
            continue
        if decision.should_complete:
            outcome = complete_settled_source_worker(agent, task)
            if outcome == "completed":
                completed += 1
            elif outcome == "pending":
                completion_pending += 1
            else:
                held += 1
            continue
        if _cancel_closed_parent_task(agent, task, decision):
            cancelled += 1
            continue
        if not decision.should_cancel:
            held += 1
    return {
        "parent_closed_cancelled": cancelled,
        "parent_recovery_held": held,
        "source_workers_completed": completed,
        "source_workers_completion_pending": completion_pending,
    }


# LLM: 父级清理不是新取消指令；只有未结束 child 可进入取消工具，不能把 FAILED 等终态改成 CANCELLED。
# 函数用途: 关闭失去父级执行权的活跃孩子，保留已经结束任务的结果、失败原因和原有用户控制记录。
def _cancel_closed_parent_task(agent: Any, task: Any, decision: Any) -> bool:
    if not decision.should_cancel or task_status_in(
        getattr(task, "status", ""),
        SUBAGENT_ENDED_STATUSES,
    ):
        return False
    if decision.reason == "parent_link_closed":
        _close_inactive_parent_audit_watches(agent, task, decision)
    from ..tools.cancel import CancelSubagentTaskRequest, cancel_subagent_task

    cancel_subagent_task(
        agent,
        CancelSubagentTaskRequest(
            task,
            f"conversation_lifecycle:{decision.reason}",
            source="parent_lifecycle_reconciler",
        ),
    )
    return True


def _close_inactive_parent_audit_watches(agent: Any, task: Any, decision: Any) -> None:
    from ....common.audit_activation import structured_audit_source_worker_attributes

    if not structured_audit_source_worker_attributes(getattr(task, "attributes", {}) or {}):
        return
    try:
        from ....conversation.named_work import close_named_audit_watches

        close_named_audit_watches(
            agent,
            decision.parent_task_id,
            reason="audit_parent_inactive",
        )
    except Exception:
        _LOGGER.warning(
            "failed to close Audit watches for inactive parent (task_id=%s)",
            decision.parent_task_id,
            exc_info=True,
        )


def complete_settled_source_worker(agent: Any, task: Any) -> str:
    """Project a naturally settled source ledger to its truthful terminal state.

    A fresh RUNNING attempt is allowed to finish through the ordinary runner
    result path, which already uses the same ledger authority.  This avoids a
    supervisor/result write race.  A stale RUNNING attempt is reclaimed by the
    liveness pass below; a later supervision tick completes the resulting
    non-running task.
    """

    from ....common.audit_activation import (
        AUDIT_SOURCE_OWNER_HOME_ATTR,
        AUDIT_SOURCE_WATCH_ID_ATTR,
        structured_audit_source_worker_attributes,
    )
    from ....ingestion.source_worker import (
        clear_source_worker_lease,
        source_worker_lifecycle_state,
    )
    from ....subagents.model_capabilities import capability_request_counts_as_open
    from ....subagents.process_control import reclaim_background_start

    manager = getattr(agent, "subagents", None)
    if manager is None:
        return "held"
    run_id = str(getattr(task, "id", "") or "").strip()
    try:
        current = manager.load(run_id)
    except Exception:
        return "held"
    attrs = dict(getattr(current, "attributes", {}) or {})
    if (
        not structured_audit_source_worker_attributes(attrs)
        or source_worker_lifecycle_state(current) != "complete"
    ):
        return "held"
    status = str(getattr(current, "status", "") or "").strip().upper()
    if status == "RUNNING":
        from ....subagents.runner_session_liveness import runner_session_of

        session_status = str(runner_session_of(current).get("status") or "").strip().lower()
        if session_status not in {"completed", "failed"}:
            # The runner-result path will mark this same task DONE/VERIFIED. If
            # it has actually hung, the liveness reconciler fences/requeues it.
            return "pending"
        # The runner-result path will mark this same task DONE/VERIFIED.  If it
        # has already ended without projecting the terminal task, it is now
        # safe for supervision to finish the ledger-derived state below.
    if (
        status == "DONE"
        and str(getattr(current, "verification_status", "") or "").strip().upper() == "VERIFIED"
    ):
        _sync_completed_source_worker_link(agent, run_id, expected_status="")
        return "completed"
    cancel_facts = attrs.get("cancel_subagents")
    cancel_facts = cancel_facts if isinstance(cancel_facts, dict) else {}
    legacy_false_cancel = (
        status == "CANCELLED"
        and str(cancel_facts.get("reason") or "")
        == "conversation_lifecycle:audit_source_watch_complete"
    )
    if status in SUBAGENT_RECOVERY_CLOSED_STATUSES and not legacy_false_cancel:
        return "held"

    now = time.time()
    active_attempt = str(getattr(current, "runner_active_attempt_id", "") or "").strip()
    if active_attempt:
        abandoned = list(getattr(current, "runner_abandoned_attempt_ids", None) or [])
        if active_attempt not in abandoned:
            abandoned.append(active_attempt)
        current.runner_abandoned_attempt_ids = abandoned
        current.runner_active_attempt_id = ""
    for request in getattr(current, "capability_requests", []) or []:
        if capability_request_counts_as_open(getattr(request, "status", "OPEN")):
            request.status = "CLOSED"
    if legacy_false_cancel:
        attrs.pop("cancel_subagents", None)
    attrs["audit_source_terminal"] = {
        "schema_version": "audit-source-terminal.v1",
        "reason": "watch_window_settled",
        "previous_status": status,
        "completed_at": now,
    }
    current.attributes = attrs
    current.status = "DONE"
    current.verification_status = "VERIFIED"
    current.failure_type = ""
    current.blockers = []
    current.progress = 1.0
    current.ended_at = now
    current.updated_at = now
    current.heartbeat_at = now
    reclaim_background_start(current)
    clear_source_worker_lease(
        Path(str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or "")),
        str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or ""),
    )
    # 仅旧版由 watch-complete 控制事件误写成 CANCELLED 的来源 worker 可以跨过
    # 终态单调保护；人工/管理员取消在上面的 closed-status 分支已经保持关闭。
    manager.save(current, allow_terminal_reactivation=legacy_false_cancel)
    _sync_completed_source_worker_link(
        agent,
        run_id,
        expected_status="cancelled" if legacy_false_cancel else "active",
    )
    manager.actions._append_task_work_log(
        current,
        "supervision: source worker DONE/VERIFIED reason=watch_window_settled",
    )
    return "completed"


def _sync_completed_source_worker_link(
    agent: Any,
    run_id: str,
    *,
    expected_status: str,
) -> None:
    store = getattr(agent, "conversation_store", None)
    update = getattr(store, "update_task_status", None)
    if not callable(update):
        return
    request = {"task_id": run_id, "status": "completed"}
    if expected_status:
        request["expected_status"] = expected_status
    try:
        update(request)
    except Exception:
        _LOGGER.debug(
            "source worker conversation completion sync failed (run_id=%s)",
            run_id,
            exc_info=True,
        )


# LLM: 续派走 dispatch 全量重评估(与模型调 dispatch_subagents 完全同一条服务链路:
#   同样的候选判定/防重/报告落盘),只是触发者从模型换成机制;no candidates 即 no-op。
# 函数用途: 用编程入口跑一次 apply+start_runners 的 dispatch,返回实际启动的 runner 数。
def _redispatch_stalled_subagents(agent: Any) -> int:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return 0
    tasks = manager.list_runs()
    decisions = conversation_lifecycle_decisions(agent, tasks)
    from ...runner.dispatch import _is_dispatch_runner_candidate

    candidates = [
        task
        for task in tasks
        if decisions.get(str(getattr(task, "id", "") or ""), None) is not None
        and decisions[str(getattr(task, "id", "") or "")].allowed
        and _is_dispatch_runner_candidate(task)
    ]
    if not candidates:
        return 0
    cfg = _dispatch_capability_config(agent)
    router = getattr(agent, "capability_router", None)
    if router is None:
        raise RuntimeError("agent capability router is unavailable")
    report = agent.dispatch_subagents(
        router,
        cfg,
        params=DispatchParams(
            apply=True,
            start_runners=True,
            max_runners=min(20, len(candidates)),
            limit=max(20, len(candidates)),
            include_run_ids=[str(task.id) for task in candidates],
            note="capability_auto_sweep: 唤醒轮机制层续派(自动批后/停滞候选)",
        ),
    )
    return _started_runner_count(report)


def _started_runner_count(report: Any) -> int:
    count = 0
    for record in getattr(report, "records", None) or []:
        if str(getattr(record, "step", "") or "") != "runner":
            continue
        if str(getattr(record, "action", "") or "") in {"started", "dispatched"}:
            count += 1
    return count


def _signal_run_id(signal: Any) -> str:
    metadata = getattr(signal, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    candidates = (
        metadata.get("run_id"),
        metadata.get("task_id"),
        getattr(signal, "source_agent_id", ""),
    )
    return next((text for value in candidates if (text := str(value or "").strip())), "")


__all__ = [
    "CAPABILITY_SWEEP_REASONS",
    "auto_capability_sweep",
    "auto_start_orphan_run",
    "auto_start_stalled_orphans",
    "complete_settled_source_worker",
    "supervise_stalled_orphans",
    "sweep_applies_to_reason",
]
