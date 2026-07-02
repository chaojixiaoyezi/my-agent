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


def sweep_applies_to_reason(reason: object) -> bool:
    return str(reason or "").strip() in CAPABILITY_SWEEP_REASONS


# 函数用途: 唤醒轮前的机制层预处理——自动批遗留 OPEN 常规申请 + 盯守死岗补建接管 run
#   + 全量续派停滞子代理(补岗建出的 PENDING 接管 run 会在同一次续派里被拉起)。
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


__all__ = ["CAPABILITY_SWEEP_REASONS", "auto_capability_sweep", "sweep_applies_to_reason"]
