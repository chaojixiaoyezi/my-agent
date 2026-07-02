# LLM: 编队持续性(补岗续盯)机制层——盯守类编队的两种真机掉链子:①盯守子代理没盯满
#   窗口就自己 DONE;②某路终态(FAILED/CANCELLED/超时)后没人接着盯。此扫描在唤醒轮
#   进 LLM 之前跑:对"窗口未走完 + 岗上 run 已终态"的盯守路,建接管 run(克隆目标/工具/
#   上下文,幂等,链深有限)→ 紧随其后的全量续派把它拉起,从持久化游标续盯。
#   判据全部结构化(watch 快照的窗口/时间戳 × run status),零自然语言判断;
#   失败绝不外抛(唤醒轮必须照常进行)。
"""Mechanism-level respawn for dead-but-incomplete watch lanes (fleet self-healing)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ....ingestion.watch_state import list_states, record_respawn
from ....subagents.models import TaskStatus
from ....subagents.services.takeover.run import TakeoverRunRequest

_LOGGER = logging.getLogger(__name__)

# 岗位已死的终态(PAUSED 是人为暂停不算死;BLOCKED 走能力自动批+续派通道,不在此列)。
_DEAD_LANE_STATUSES = frozenset({
    TaskStatus.DONE.value,
    TaskStatus.FAILED.value,
    TaskStatus.TIMEOUT.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.ABANDONED.value,
    TaskStatus.CHANNEL_ERROR.value,
    TaskStatus.TAKEN_OVER.value,
})
_TAKEOVER_CHAIN_FOLLOW_CAP = 8


def respawn_dead_watch_lanes(agent: Any) -> list[dict[str, object]]:
    """扫一遍本 owner 的盯守路;给死岗建接管 run 并【立即经 auto_start 拉起】。返回补岗动作清单。

    拉起用 auto_start_tasks(create_subagents 同款 durable 后台派工路径),不依赖唤醒轮上下文的
    start_runners——真机实测:唤醒轮里靠 _redispatch 拉 PLANNING 接管 run 不稳(停在 PLANNING),
    而 auto_start 起的 5 条健康盯守路全部跑起来了,故补岗走同一条可靠路。
    """
    owner_home = _owner_home(agent)
    manager = getattr(agent, "subagents", None)
    if owner_home is None or manager is None:
        return []
    actions: list[dict[str, object]] = []
    takeover_ids: list[str] = []
    for lane in list_states(owner_home):
        action = _respawn_lane_if_dead(manager, owner_home, lane)
        if action is not None:
            actions.append(action)
            _observe_respawn(agent, action)
            takeover_ids.append(str(action.get("takeover_run_id") or ""))
    _auto_start_takeovers(agent, manager, [rid for rid in takeover_ids if rid])
    return actions


def _auto_start_takeovers(agent: Any, manager: Any, takeover_ids: list[str]) -> None:
    """经 create_subagents 同款 auto_start 路径拉起新建的接管 run(比唤醒轮 redispatch 可靠)。"""
    if not takeover_ids:
        return
    try:
        from ..background.dispatch import auto_start_tasks

        tasks = [_load_task(manager, rid) for rid in takeover_ids]
        auto_start_tasks(agent, [task for task in tasks if task is not None], {})
    except Exception:
        _LOGGER.warning("watch lane takeover auto-start failed", exc_info=True)


def _respawn_lane_if_dead(manager: Any, owner_home: Path, lane: dict[str, Any]) -> dict[str, object] | None:
    if not _lane_needs_watching(lane):
        return None
    puller = str(lane.get("last_puller_run_id") or "").strip()
    if not puller:
        return None
    duty_run = _chain_end_task(manager, puller)
    if duty_run is None:
        return None
    status = str(getattr(duty_run, "status", "") or "")
    if status not in _DEAD_LANE_STATUSES:
        return None
    return _create_respawn(manager, owner_home, lane, duty_run)


def _lane_needs_watching(lane: dict[str, Any]) -> bool:
    """窗口未走完的活跃盯守路(全结构化:closed/窗口时长/已流逝时间)。"""
    if bool(lane.get("closed")):
        return False
    window = int(lane.get("watch_window_seconds") or 0)
    if window <= 0:
        return False
    elapsed = time.time() - float(lane.get("opened_at") or 0.0)
    return elapsed < window


def _chain_end_task(manager: Any, run_id: str) -> Any | None:
    """沿 takeover 链走到最新接管者(接管者又死了要接管接管者,链深由 takeover 服务封顶)。"""
    current_id = run_id
    task = None
    for _hop in range(_TAKEOVER_CHAIN_FOLLOW_CAP):
        task = _load_task(manager, current_id)
        if task is None:
            return None
        next_id = str(getattr(task, "takeover_by", "") or "").strip()
        if not next_id or next_id == current_id:
            return task
        current_id = next_id
    return task


def _load_task(manager: Any, run_id: str) -> Any | None:
    try:
        return manager.load(run_id)
    except FileNotFoundError:
        return None  # 非子代理 run(如主代理 solo 盯守)不归本扫描管
    except Exception:
        _LOGGER.warning("watch lane sweep load failed (run_id=%s)", run_id, exc_info=True)
        return None


def _create_respawn(manager: Any, owner_home: Path, lane: dict[str, Any], duty_run: Any) -> dict[str, object] | None:
    watch_id = str(lane.get("watch_id") or "")
    remaining = _remaining_seconds(lane)
    reason = (
        f"盯守补岗: watch {watch_id} 窗口未走完(剩余约 {remaining}s)而岗上 run "
        f"{getattr(duty_run, 'id', '')} 已终态({getattr(duty_run, 'status', '')});"
        f"从持久化游标续盯到窗口结束(watch_stream action=open 同源即续)。"
    )
    try:
        result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=str(duty_run.id), reason=reason))
    except Exception:
        _LOGGER.warning("watch lane respawn failed (watch=%s)", watch_id, exc_info=True)
        return None
    if not getattr(result, "created", False):
        return None  # 已有活着的接管者(幂等)或链深封顶:不重复建岗
    record_respawn(owner_home, watch_id, str(getattr(result, "takeover_run_id", "") or ""))
    return {
        "watch_id": watch_id,
        "source_run_id": str(getattr(result, "source_run_id", "") or ""),
        "takeover_run_id": str(getattr(result, "takeover_run_id", "") or ""),
        "remaining_seconds": remaining,
        "dead_status": str(getattr(duty_run, "status", "") or ""),
    }


def _remaining_seconds(lane: dict[str, Any]) -> int:
    window = int(lane.get("watch_window_seconds") or 0)
    elapsed = time.time() - float(lane.get("opened_at") or 0.0)
    return max(0, int(window - elapsed))


def _observe_respawn(agent: Any, action: dict[str, object]) -> None:
    """把补岗动作写进会话观察流(验收口径:日志可见"某路终态→补派续盯"动作)。绝不抛。"""
    store = getattr(agent, "conversation_store", None)
    source_id = str(action.get("source_run_id") or "")
    if store is None or not source_id:
        return
    try:
        thread = store.thread_for_task(source_id)
        if thread is None:
            return
        store.append_observation({
            "thread_id": thread.thread_id,
            "event_type": "watch_lane_respawned",
            "summary": (
                f"盯守补岗: watch {action.get('watch_id')} 的岗上 run {source_id} 已终态"
                f"({action.get('dead_status')}),窗口还剩 ~{action.get('remaining_seconds')}s,"
                f"已自动建接管 run {action.get('takeover_run_id')} 续盯(游标断点续)。"
            ),
            "urgency": "normal",
            "source_agent_id": source_id,
            "requires_main_agent": False,
            "metadata": dict(action),
        })
    except Exception:
        _LOGGER.debug("watch lane respawn observation failed", exc_info=True)


def _owner_home(agent: Any) -> Path | None:
    raw = str(getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or "").strip()
    return Path(raw) if raw else None


__all__ = ["respawn_dead_watch_lanes"]
