"""盯守自唤醒节奏兜底:backlog 有活堆着时,给自唤醒排期一个结构化响应上限。

真机实锤(§8.3):同一盯守任务两个用户冰火两重天——模型一个选 60s 自唤醒(funnelB 271),
另一个选 2700s(拉一次睡到窗口末:funnelB 冻死 4、consumed 死钉、backlog 52→231 没人判)。
盯守续航 100% 挂在"模型自己选对间隔"这个不稳定行为上,系统无结构兜底——间隔一旦排得
过长,盯守静默失效(候选堆着没人判、用户全程收不到)。与 A2 同源教训:别拿不稳的模型
行为扛续航。

兜底两半(复用判读吞吐反压已在算的同一结构信号 backlog = spool_candidates − candidates_consumed):
① 排期封顶(expedite):owner 有活跃未判读积压时,enabled 盯守 policy 的 next_due_at
   距今不得超过响应上限(subagent_watch_interval_seconds,已有配置、零新参数);只单调
   提前、不改模型的 interval_seconds——不禁止模型设间隔,backlog 清零后自动回到模型
   自选节奏。常态零盘 IO:没有"睡过头"的盯守 policy 就不读 watch 快照。
② 排期自愈(rebuild):窗口未到期的活跃 backlog 路,连 enabled 盯守 policy 都没了
   (被 cancel/收口误退休)且该线程无 running claim → 机制层重建一个响应上限间隔的
   兜底 policy(dispatch_supervision_auto 自动登记同款先例),别等模型下一次醒。

铁律:判断只用结构化信号(计数/时间戳/状态/kind 枚举),零自然语言;失败绝不外抛
(唤醒轮/supervision 必须照常进行)。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report
from .harvester import _WINDOW_GRACE_SECONDS
from .watch_state import list_states, state_dir

_LOGGER = logging.getLogger(__name__)

# 只兜内部自唤醒的盯守类 policy(wait 工具/机制层监督登记的 kind);
# 外呼渠道的用户定期汇报 policy 是用户显式节奏,不属于盯守续航,绝不碰。
_WATCH_POLICY_KIND = "subagent_progress_watch"
_BACKSTOP_TOOL = "watch_backlog_backstop"
# 响应上限下限 = wait 工具允许的最短间隔(_MIN_SECONDS 同值):兜底不比模型能设的最短更激进。
_MIN_CAP_SECONDS = 60
_DEFAULT_CAP_SECONDS = 120


def watch_response_cap_seconds(agent: Any) -> int:
    """backlog 未清时的自唤醒响应上限;复用已有配置 subagent_watch_interval_seconds。"""
    configured = getattr(getattr(agent, "config", None), "subagent_watch_interval_seconds", _DEFAULT_CAP_SECONDS)
    try:
        cap = int(configured)
    except (TypeError, ValueError):
        cap = _DEFAULT_CAP_SECONDS
    return max(_MIN_CAP_SECONDS, cap)


def expedite_watch_policies_for_backlog(
    agent: Any, store: Any, policies: list[Any], *, now: float | None = None
) -> list[dict[str, object]]:
    """①排期封顶:睡过头的盯守 policy × owner 有活跃 backlog → next_due_at 钳到 now+cap。

    调度器每 tick 传入本轮已列出的 enabled policies(零额外列账 IO);返回动作行(观测用)。
    绝不外抛。
    """
    try:
        return _expedite(agent, store, policies, now if now is not None else time.time())
    except Exception:
        _LOGGER.debug("watch backlog expedite failed", exc_info=True)
        return []


def _expedite(agent: Any, store: Any, policies: list[Any], now: float) -> list[dict[str, object]]:
    if not callable(getattr(store, "expedite_progress_policy", None)):
        return []
    cap = watch_response_cap_seconds(agent)
    sleepy = [p for p in policies if _is_watch_policy(p) and (float(p.next_due_at) - now) > cap]
    if not sleepy:
        return []
    owner_home = _owner_home(agent)
    if owner_home is None or not _has_active_backlog(owner_home, now):
        return []
    actions: list[dict[str, object]] = []
    for policy in sleepy:
        updated = store.expedite_progress_policy(
            policy.policy_id, due_at=now + cap, reason="watch_backlog", now=now
        )
        if updated is None:
            continue
        actions.append(
            {
                "action": "expedited",
                "policy_id": policy.policy_id,
                "task_id": policy.task_id,
                "was_due_in_seconds": int(float(policy.next_due_at) - now),
                "capped_to_seconds": cap,
            }
        )
    if actions:
        _LOGGER.info("watch backlog backstop expedited %d policy(ies) to %ds", len(actions), cap)
    return actions


def rebuild_missing_watch_policies(agent: Any, *, now: float | None = None) -> list[dict[str, object]]:
    """②排期自愈:窗口内活跃 backlog 路 + owner 连 enabled 盯守 policy 都没了 + 线程无
    running claim → 机制层重建兜底 policy。由 supervision 节奏召唤;有任何 enabled 盯守
    policy 即短路(那是①的场,零盘 IO)。绝不外抛。
    """
    try:
        return _rebuild(agent, now if now is not None else time.time())
    except Exception:
        _LOGGER.debug("watch policy rebuild failed", exc_info=True)
        return []


def _rebuild(agent: Any, now: float) -> list[dict[str, object]]:
    store = getattr(agent, "conversation_store", None)
    owner_home = _owner_home(agent)
    if store is None or owner_home is None:
        return []
    if not callable(getattr(store, "set_progress_policy", None)) or not callable(
        getattr(store, "list_progress_policies", None)
    ):
        return []
    if any(_is_watch_policy(p) for p in store.list_progress_policies(enabled_only=True)):
        return []
    cap = watch_response_cap_seconds(agent)
    actions: list[dict[str, object]] = []
    seen_pullers: set[str] = set()  # 同一消费者的多路流只重建一份(唤醒轮会 pull 所有路)
    for lane in list_states(owner_home):
        if not _lane_rebuild_eligible(owner_home, lane, now):
            continue
        puller = str(lane.get("last_puller_run_id") or "").strip()
        if not puller or puller in seen_pullers:
            continue
        thread_id = _rebuild_target_thread(store, puller, now)
        if not thread_id:
            continue
        policy = store.set_progress_policy(_backstop_policy_request(thread_id, puller, lane, cap))
        seen_pullers.add(puller)
        action: dict[str, object] = {
            "action": "policy_rebuilt",
            "watch_id": str(lane.get("watch_id") or ""),
            "policy_id": str(getattr(policy, "policy_id", "") or ""),
            "thread_id": thread_id,
            "task_id": puller,
            "interval_seconds": cap,
        }
        actions.append(action)
        _observe_rebuild(store, action)
    return actions


def _lane_rebuild_eligible(owner_home: Path, lane: dict[str, Any], now: float) -> bool:
    # 仅有窗且窗口未到期的活跃 backlog 路(与补岗扫描同口径,保守):无窗长守的边界交给
    # policy 生命周期(任务终态退休)兜,不在这里无限重建。
    if bool(lane.get("closed")):
        return False
    window = int(lane.get("watch_window_seconds") or 0)
    if window <= 0 or (now - float(lane.get("opened_at") or 0.0)) >= window:
        return False
    return _lane_backlog(owner_home, lane) > 0


def _rebuild_target_thread(store: Any, puller: str, now: float) -> str:
    """兜底 policy 的落点线程:消费者 run 关联不上线程就不瞎建(诚实跳过;①仍兜
    "policy 还在"的主场景);线程正有 running claim(唤醒轮在跑)也不建。"""
    thread = _thread_for_task(store, puller)
    thread_id = str(getattr(thread, "thread_id", "") or "")
    if not thread_id or _thread_claim_running(store, thread_id, now):
        return ""
    return thread_id


def _backstop_policy_request(thread_id: str, puller: str, lane: dict[str, Any], cap: int) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "task_id": puller,
        "interval_seconds": cap,
        "route_channel": "internal",
        "route_target": "",
        "metadata": {
            "kind": _WATCH_POLICY_KIND,
            "tool": _BACKSTOP_TOOL,
            "scope": "own_task_tree",
            "reason": (
                f"盯守排期自愈: watch {lane.get('watch_id')} 窗口未到期且 spool 有未判读候选,"
                "但没有任何在排期的自唤醒提醒——机制层重建;醒来后继续 pull 消费并逐条判读上报。"
            ),
            "watch_run_id": puller,
        },
    }


# ---------------------------------------------------------------------------
# 结构信号读取(纯文件,不复活引擎、不碰 registry 锁)
# ---------------------------------------------------------------------------


def _has_active_backlog(owner_home: Path, now: float) -> bool:
    return any(_lane_backlog(owner_home, lane) > 0 for lane in list_states(owner_home) if _lane_active(lane, now))


def _lane_active(lane: dict[str, Any], now: float) -> bool:
    """未关闭且(无窗长守 或 窗口+收尾余量内)的盯守路;窗口末余量与 harvester 停机口径一致,
    让最后一批候选也有人来判。"""
    if bool(lane.get("closed")):
        return False
    window = int(lane.get("watch_window_seconds") or 0)
    if window <= 0:
        return True
    elapsed = now - float(lane.get("opened_at") or 0.0)
    return elapsed <= window + _WINDOW_GRACE_SECONDS


def _lane_backlog(owner_home: Path, lane: dict[str, Any]) -> int:
    written = int((lane.get("totals") or {}).get("spool_candidates") or 0)
    if written <= 0:
        return 0
    return max(0, written - _consumed_candidates(owner_home, str(lane.get("watch_id") or "")))


def _consumed_candidates(owner_home: Path, watch_id: str) -> int:
    if not watch_id:
        return 0
    report = read_json_object_report(
        state_dir(owner_home) / f"{watch_id}.read.json", context="watch_wake_backstop.read_cursor"
    )
    if report.load_error is not None:
        return 0
    try:
        return max(0, int(report.payload.get("candidates_consumed") or 0))
    except (TypeError, ValueError):
        return 0


def _is_watch_policy(policy: Any) -> bool:
    metadata = getattr(policy, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("kind") or "") == _WATCH_POLICY_KIND


def _thread_for_task(store: Any, task_id: str) -> Any | None:
    if not callable(getattr(store, "thread_for_task", None)):
        return None
    try:
        return store.thread_for_task(task_id)
    except Exception:
        return None


def _thread_claim_running(store: Any, thread_id: str, now: float) -> bool:
    if not callable(getattr(store, "load_background_run_claim", None)):
        return False
    try:
        claim = store.load_background_run_claim(thread_id)
    except Exception:
        return False
    if not isinstance(claim, dict) or str(claim.get("status") or "") != "running":
        return False
    try:
        return float(claim.get("expires_at") or 0.0) > now
    except (TypeError, ValueError):
        return False


def _observe_rebuild(store: Any, action: dict[str, object]) -> None:
    """自愈动作写会话观察流(验收/排障可见"排期断了→机制层重建"),绝不抛。"""
    if not callable(getattr(store, "append_observation", None)):
        return
    try:
        store.append_observation(
            {
                "thread_id": str(action.get("thread_id") or ""),
                "event_type": "watch_policy_rebuilt",
                "summary": (
                    f"盯守排期自愈: watch {action.get('watch_id')} 有未判读积压但没有任何在排期的"
                    f"自唤醒提醒,已机制层重建 policy {action.get('policy_id')}"
                    f"(间隔 {action.get('interval_seconds')}s)续判。"
                ),
                "urgency": "normal",
                "source_agent_id": str(action.get("task_id") or ""),
                "requires_main_agent": False,
                "metadata": dict(action),
            }
        )
    except Exception:
        _LOGGER.debug("watch policy rebuild observation failed", exc_info=True)


def _owner_home(agent: Any) -> Path | None:
    raw = str(getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or "").strip()
    return Path(raw) if raw else None


__all__ = [
    "expedite_watch_policies_for_backlog",
    "rebuild_missing_watch_policies",
    "watch_response_cap_seconds",
]
