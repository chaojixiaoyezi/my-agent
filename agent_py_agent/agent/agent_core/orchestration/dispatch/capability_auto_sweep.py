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
from typing import Any

from agent_py_agent.agent.capability import CapabilityRouter

from ....subagents.capability_auto_grant import auto_grant_open_requests
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
        stalled = [task for task in manager.list_runs() if _is_stalled_dispatchable_orphan(task)]
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
def _is_stalled_dispatchable_orphan(task: Any) -> bool:
    from ....contracts.state_machine import DISPATCHABLE_STATES, normalize_status
    from ....subagents.runner_session_liveness import has_fresh_runner_session
    from ...runner.dispatch import _is_dispatch_runner_candidate

    if normalize_status(str(getattr(task, "status", "") or "")) not in DISPATCHABLE_STATES:
        return False
    if int(getattr(task, "runner_attempts", 0) or 0) >= _ORPHAN_REVIVE_ATTEMPT_CAP:
        return False
    if has_fresh_runner_session(task):
        return False
    # 复用派工候选判定(launch 防重/open 能力申请/gap/verified 排除),与 dispatch 同一口径。
    return _is_dispatch_runner_candidate(task)


# LLM: 周期性 supervision(worker-pool self-healing 的 reconcile 半边):事件唤醒(wake)
#   只覆盖"有人发信号"的死亡;宿主进程被 SIGKILL/断电类静默死亡不发任何 wake,靠这里
#   周期兜底。动作=盯守死岗补建接管 + durable 复活可派孤儿,零 LLM 成本、无候选即 no-op。
# 函数用途: 后台调度器/定时提醒路的机制层巡查:把静默死掉的岗位和孤儿捡回来。
def supervise_stalled_orphans(agent: Any) -> dict[str, object]:
    summary: dict[str, object] = {"watch_respawned": 0, "orphans_revived": 0}
    try:
        from .watch_lane_sweep import respawn_dead_watch_lanes

        summary["watch_respawned"] = len(respawn_dead_watch_lanes(agent))
    except Exception:
        _LOGGER.debug("supervision watch-lane respawn failed", exc_info=True)
    try:
        summary["orphans_revived"] = int(auto_start_stalled_orphans(agent).get("started") or 0)
    except Exception:
        _LOGGER.debug("supervision orphan revive failed", exc_info=True)
    return summary


# LLM: 续派走 dispatch 全量重评估(与模型调 dispatch_subagents 完全同一条服务链路:
#   同样的候选判定/防重/报告落盘),只是触发者从模型换成机制;no candidates 即 no-op。
# 函数用途: 用编程入口跑一次 apply+start_runners 的 dispatch,返回实际启动的 runner 数。
def _redispatch_stalled_subagents(agent: Any) -> int:
    cfg = _dispatch_capability_config(agent)
    tool_specs = [spec for spec in agent.tools.specs() if spec.category != "orchestration"]
    router = CapabilityRouter(config=cfg, tool_specs=tool_specs)
    report = agent.dispatch_subagents(
        router,
        cfg,
        apply=True,
        start_runners=True,
        note="capability_auto_sweep: 唤醒轮机制层续派(自动批后/停滞候选)",
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
