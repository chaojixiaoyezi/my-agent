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
"""Mechanism-level capability sweep before subagent-lifecycle wake turns."""

import logging
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from agent_py_agent.agent.capability import CapabilityRouter

from ....subagents.capability_auto_grant import auto_grant_open_requests
from ....subagents.models import SUBAGENT_RECOVERY_CLOSED_STATUSES, task_status_in
from .conversation_lifecycle_gate import conversation_lifecycle_decisions
from .params import DispatchParams
from .tool_helpers import _dispatch_capability_config

_LOGGER = logging.getLogger(__name__)

# 与 runner_completion_wake / capability_request_tool 发出的 reason 对齐。
CAPABILITY_SWEEP_REASONS = frozenset({
    "subagent_capability_request_open",
    "subagent_runner_finished",
})

# 机制层自动复活孤儿的尝试上限:与 recovery/strategy 的 no_progress_attempt_limit(4)对齐,
# 反复起不来的 run 留给模型层裁决(takeover/cancel),不做无限机械重启。
_ORPHAN_REVIVE_ATTEMPT_CAP = 4


def sweep_applies_to_reason(reason: object) -> bool:
    return str(reason or "").strip() in CAPABILITY_SWEEP_REASONS


# 函数用途: 唤醒轮前的机制层预处理——自动批遗留 OPEN 常规申请 + 盯守死岗补建接管 run
#   + 全量续派停滞子代理(补岗建出的 PENDING 接管 run 会在同一次续派里被拉起)。
#   注:可派孤儿的 durable 复活不在这条唤醒热路径上做(同步续派已覆盖同批候选),
#   由出口回收就地复活 + 调度器周期 supervision + 定时提醒 sweep 三条常驻路兜底。
def auto_capability_sweep(agent: Any, signal: Any) -> dict[str, object]:
    summary: dict[str, object] = {"auto_granted": 0, "redispatched": 0, "watch_respawned": 0}
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return summary
    run_id = _signal_run_id(signal)
    if run_id:
        try:
            grants = auto_grant_open_requests(manager, run_id, extra_safe_roots=_manager_safe_roots(manager))
            summary["auto_granted"] = len(grants)
        except Exception:
            _LOGGER.warning("capability auto sweep grant failed (run_id=%s)", run_id, exc_info=True)
    try:
        from .watch_lane_sweep import respawn_dead_watch_lanes

        summary["watch_respawned"] = len(respawn_dead_watch_lanes(agent))
    except Exception:
        _LOGGER.warning("capability auto sweep watch-lane respawn failed", exc_info=True)
    try:
        summary["redispatched"] = _redispatch_stalled_subagents(agent)
    except Exception:
        _LOGGER.warning("capability auto sweep redispatch failed", exc_info=True)
    return summary


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
        stalled = [
            task
            for task in tasks
            if _is_stalled_dispatchable_orphan(
                task,
                decisions.get(str(getattr(task, "id", "") or "")),
            )
        ]
    except Exception:
        _LOGGER.warning("orphan revive list_runs failed", exc_info=True)
        return {"started": 0}
    if not stalled:
        return {"started": 0}
    from ..background.dispatch import auto_start_tasks

    result = auto_start_tasks(agent, stalled, {})
    started = list(result.get("run_ids") or []) if str(result.get("status") or "") == "started" else []
    return {"started": len(started), "status": str(result.get("status") or ""), "run_ids": started}


# 函数用途: 判断一个 run 是不是"该被机制层复活的停滞可派孤儿"(全结构化判据)。
def _is_stalled_dispatchable_orphan(task: Any, decision: Any = None) -> bool:
    from ....contracts.state_machine import DISPATCHABLE_STATES, normalize_status
    from ....subagents.runner_session_liveness import has_fresh_runner_session
    from ...runner.dispatch import _is_dispatch_runner_candidate

    if normalize_status(str(getattr(task, "status", "") or "")) not in DISPATCHABLE_STATES:
        return False
    if int(getattr(task, "runner_attempts", 0) or 0) >= _ORPHAN_REVIVE_ATTEMPT_CAP:
        return False
    if has_fresh_runner_session(task):
        return False
    if decision is None or not decision.allowed:
        return False
    # 复用派工候选判定(launch 防重/open 能力申请/gap/verified 排除),与 dispatch 同一口径。
    return _is_dispatch_runner_candidate(task)


# LLM: 周期性 supervision(worker-pool self-healing 的 reconcile 半边):事件唤醒(wake)
#   只覆盖"有人发信号"的死亡;宿主进程被 SIGKILL/断电/网关重启类静默死亡不发任何 wake,
#   靠这里周期兜底。动作=回收宿主已死的 RUNNING(requeue)→ 盯守死岗补建接管 →
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
        stack.enter_context(_DispatchWatchLock(Path(workspace) / "subagent_orphan_supervision.lock"))
    except RuntimeError:
        # Another trigger path is already reconciling this exact owner workspace.
        # It will persist the new canonical state before releasing the lock; the
        # next scheduled pass recomputes from disk instead of duplicating starts.
        return {
            "running_reclaimed": 0,
            "watch_respawned": 0,
            "orphans_revived": 0,
            "skipped_locked": 1,
        }
    with stack:
        return _supervise_stalled_orphans_unlocked(agent)


def _supervise_stalled_orphans_unlocked(agent: Any) -> dict[str, object]:
    summary: dict[str, object] = {
        "parent_closed_cancelled": 0,
        "parent_recovery_held": 0,
        "running_reclaimed": 0,
        "watch_respawned": 0,
        "orphans_revived": 0,
    }
    try:
        parent_summary = _reconcile_conversation_parent_lifecycle(agent)
        summary.update(parent_summary)
    except Exception:
        _LOGGER.debug("supervision parent lifecycle reconcile failed", exc_info=True)
    try:
        summary["running_reclaimed"] = len(_reclaim_dead_running_runs(agent))
    except Exception:
        _LOGGER.debug("supervision running reclaim failed", exc_info=True)
    try:
        from .watch_lane_sweep import respawn_dead_watch_lanes

        summary["watch_respawned"] = len(respawn_dead_watch_lanes(agent))
    except Exception:
        _LOGGER.debug("supervision watch-lane respawn failed", exc_info=True)
    try:
        # §8.3 盯守排期自愈②:窗口未到期的活跃 backlog 路连 enabled 盯守 policy 都没了
        # (cancel/收口误退休)→ 机制层重建兜底 policy;有任何 enabled 盯守 policy 即短路。
        from ....ingestion.wake_backstop import rebuild_missing_watch_policies

        summary["watch_policies_rebuilt"] = len(rebuild_missing_watch_policies(agent))
    except Exception:
        _LOGGER.debug("supervision watch policy rebuild failed", exc_info=True)
    try:
        summary["orphans_revived"] = int(auto_start_stalled_orphans(agent).get("started") or 0)
    except Exception:
        _LOGGER.debug("supervision orphan revive failed", exc_info=True)
    return summary


# LLM: 宿主已死的 RUNNING 回收(重启/SIGKILL 韧性的最后一环):RUNNING 但 runner 会话
#   心跳过期【且】会话宿主 pid 已死 → runner 线程必已消亡,abandon 当前 attempt 并
#   requeue PENDING(同一轮 supervision 的复活扫描随即拉起续跑)。判据全结构化且双重
#   保守:心跳新鲜不动;宿主 pid 还活着也不动(可能只是心跳抖动/长 GC,交给出口回收
#   的活性豁免链处置);无会话事实的老数据不动。
# 函数用途: 网关重启/进程被杀后,把"看着在跑其实早死了"的 run 放回队列续命。
def _reclaim_dead_running_runs(agent: Any) -> list[str]:
    from ....subagents.process_control import PROCESS_EPOCH, is_pid_alive
    from ....subagents.runner_session_liveness import has_fresh_runner_session, runner_session_of

    manager = getattr(agent, "subagents", None)
    if manager is None:
        return []
    reclaimed: list[str] = []
    tasks = manager.list_runs()
    decisions = conversation_lifecycle_decisions(agent, tasks)
    for task in tasks:
        if str(getattr(task, "status", "") or "").strip().upper() != "RUNNING":
            continue
        decision = decisions.get(str(getattr(task, "id", "") or ""))
        if decision is None or not decision.allowed:
            continue
        session = runner_session_of(task)
        if not session or has_fresh_runner_session(task):
            continue
        # completed/failed 是 runner 自己持久化的正常终止事实，不是宿主猝死。
        # 旧实现把所有“非 fresh”会话一概当死进程回收，导致 canonical task 偶发残留
        # RUNNING 时，每次网关重启都会重新执行一遍已经结束的 runner。这里只接管
        # starting/running 且失去心跳的会话；显式终态留给结果/生命周期调和链处理。
        session_status = str(session.get("status") or "").strip()
        if session_status not in {"starting", "running"}:
            continue
        try:
            worker_pid = int(session.get("worker_pid") or 0)
        except (TypeError, ValueError):
            worker_pid = 0
        # 到这里心跳已过期(has_fresh_runner_session 判过)= 宿主 45s+ 没跳,权威地判死。
        # pid-liveness 作 GC 抖动豁免(worker_pid 活着可能只是长 GC,不抢)。但【in-process
        # runner 换代】例外:它随记录它的网关进程存亡,网关重启后 worker_pid 是旧网关的(可能被
        # 复用/EPERM 而误判"活"),会把随旧进程消亡的 in-process runner 永冻——异代 in-process
        # 直接按心跳过期回收、不看 pid。独立派工子进程(in_process=False)不随网关重启死,仍走
        # pid-liveness 保留 GC 豁免,不被换代误杀;无 in_process/epoch 的旧数据同样保守走 pid。
        session_epoch = str(session.get("process_epoch") or "")
        cross_gen_inprocess = bool(session.get("in_process")) and bool(session_epoch) and session_epoch != PROCESS_EPOCH
        if not cross_gen_inprocess and (worker_pid <= 0 or is_pid_alive(worker_pid)):
            continue
        run_id = str(getattr(task, "id", "") or "")
        try:
            _requeue_dead_running(manager, task, run_id)
            reclaimed.append(run_id)
        except Exception:
            _LOGGER.warning("supervision reclaim failed (run_id=%s)", run_id, exc_info=True)
    return reclaimed


# 函数用途: 单个宿主已死 run 的 requeue(abandon attempt → PENDING → 留结构化痕迹)。
def _requeue_dead_running(manager: Any, task: Any, run_id: str) -> None:
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if attempt_id:
        manager.lifecycle.abandon_runner_attempt(run_id, attempt_id, reason="supervision_dead_worker_reclaim")
    refreshed = manager.load(run_id)
    refreshed.status = "PENDING"
    refreshed.failure_type = ""
    _clear_background_start_residue(refreshed)
    manager.save(refreshed)
    manager.actions._append_task_work_log(
        refreshed,
        "supervision: requeued RUNNING->PENDING reason=dead_worker_session",
    )


# 函数用途: 把宿主已死 run 的 background_start 残留(launching/running)标成 reclaimed。
#   不清则 requeue 出的 PENDING 又被候选判定的 runner_launch_in_progress 按残留状态排除,
#   回收等于白做(P2 真机实锤:重启后 PENDING 卡死)。经权威构造更新,pid 记录不丢。
def _clear_background_start_residue(task: Any) -> None:
    from ....subagents.process_control import BackgroundStartUpdate, build_background_start_record

    attrs = getattr(task, "attributes", None)
    if not isinstance(attrs, dict):
        return
    background = attrs.get("background_start")
    if not isinstance(background, dict):
        return
    if str(background.get("status") or "").strip() not in {"launching", "running"}:
        return
    attrs["background_start"] = build_background_start_record(
        background,
        BackgroundStartUpdate(launch_id=str(background.get("launch_id") or ""), status="reclaimed"),
    )


def _reconcile_conversation_parent_lifecycle(agent: Any) -> dict[str, int]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return {"parent_closed_cancelled": 0, "parent_recovery_held": 0}
    tasks = manager.list_runs()
    decisions = conversation_lifecycle_decisions(agent, tasks)
    cancelled = 0
    held = 0
    for task in tasks:
        decision = decisions.get(str(getattr(task, "id", "") or ""))
        if decision is None or decision.allowed:
            continue
        if decision.should_cancel and not task_status_in(
            getattr(task, "status", ""),
            SUBAGENT_RECOVERY_CLOSED_STATUSES,
        ):
            from ..tools.cancel import CancelSubagentTaskRequest, cancel_subagent_task

            cancel_subagent_task(
                agent,
                CancelSubagentTaskRequest(
                    task,
                    f"conversation_lifecycle:{decision.reason}",
                    source="parent_lifecycle_reconciler",
                ),
            )
            cancelled += 1
            continue
        if not decision.should_cancel:
            held += 1
    return {"parent_closed_cancelled": cancelled, "parent_recovery_held": held}


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
    tool_specs = [spec for spec in agent.tools.specs() if spec.category != "orchestration"]
    router = CapabilityRouter(config=cfg, tool_specs=tool_specs)
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
    candidates = (metadata.get("run_id"), metadata.get("task_id"), getattr(signal, "source_agent_id", ""))
    return next((text for value in candidates if (text := str(value or "").strip())), "")


def _manager_safe_roots(manager: Any) -> tuple[str, ...]:
    roots = []
    for raw in (getattr(manager, "workspace_root", ""), getattr(manager, "workspace", "")):
        text = str(raw or "").strip()
        if text:
            roots.append(text)
    return tuple(roots)


__all__ = [
    "CAPABILITY_SWEEP_REASONS",
    "auto_capability_sweep",
    "auto_start_stalled_orphans",
    "supervise_stalled_orphans",
    "sweep_applies_to_reason",
]
