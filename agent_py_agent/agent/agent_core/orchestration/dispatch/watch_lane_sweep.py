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

from ....ingestion.wake_backstop import (
    lane_last_consumed_at,
    lane_unjudged_backlog,
    watch_response_cap_seconds,
)
from ....ingestion.watch_state import list_states, record_respawn, state_dir
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
# 非终态但"本该被驱动却卡住"的状态(复活通道的正常受众):PENDING/PLANNING 待启动孤儿、
# BLOCKED 待解阻。RUNNING(活着或宿主已死的僵尸,归 _reclaim_dead_running_runs 回收)与
# PAUSED(人为暂停)不在此列——绝不由本扫描接管。
_STALLED_NONTERMINAL_STATUSES = frozenset({
    TaskStatus.PLANNING.value,
    TaskStatus.PENDING.value,
    TaskStatus.BLOCKED.value,
})
# 消费新鲜窗(×自唤醒响应上限):最近一次拉取/读游标推进在这窗内 = 有人在岗,不补岗。
# 唤醒兜底保证有积压时拉取节拍不超过响应上限,×2 容忍一个长判读轮的间隙。
_CONSUMPTION_FRESH_FACTOR = 2
# DONE+排期在场的续驱容忍上限(×响应上限):终态是 DONE 且 run 名下还有 enabled 循环提醒
# 时,唤醒续驱会把下一轮判读带回来——但消费停摆超过这个窗就当续驱已僵死,照常补岗(防饿死)。
_DRIVEN_STALE_FACTOR = 6


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
        action = _respawn_lane_if_dead(agent, manager, owner_home, lane)
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


def _respawn_lane_if_dead(agent: Any, manager: Any, owner_home: Path, lane: dict[str, Any]) -> dict[str, object] | None:
    if not _lane_needs_watching(owner_home, lane):
        return None
    puller = str(lane.get("last_puller_run_id") or "").strip()
    if not puller:
        return None
    duty_run = _chain_end_task(manager, puller)
    if duty_run is None:
        return None
    status = str(getattr(duty_run, "status", "") or "")
    if status in _DEAD_LANE_STATUSES:
        if _lane_still_manned(agent, owner_home, lane, duty_run, status):
            return None
        return _create_respawn(manager, owner_home, lane, duty_run)
    # 非终态但卡死(P2 重启只恢复一部分源的真因):puller 名义上还 PENDING/PLANNING/BLOCKED,
    # 却既不活(无 fresh session/running claim)、消费又长期停摆,而孤儿复活会因 attempt 上限
    # /能力缺口/channel BROKEN 等排除【拉不起来】——这一路便无任何机制驱动(补岗只认终态、
    # 复活又排除它)。churn 越多(接管来回)越容易把部分源的 run 打进这个夹缝,真机 5 源
    # 只 2 源恢复即此。建接管(fresh run,attempt 归零)打破死锁;能被复活的(未过闸)不碰,
    # 避免与 auto_start 双驱。终点仍是积压清零/显式 close,不会永续。
    if _lane_stuck_nonterminal(agent, owner_home, lane, duty_run):
        return _create_respawn(manager, owner_home, lane, duty_run)
    return None


def _lane_stuck_nonterminal(agent: Any, owner_home: Path, lane: dict[str, Any], duty_run: Any) -> bool:
    """非终态 puller 是否已卡死到必须接管:待启动/待解阻态 + 消费长期停摆 + 不活 +
    孤儿复活也拉不起来。全结构化;任一读取失败保守 False(不接管,回落旧行为=交给复活/
    唤醒/回收兜底)。RUNNING/PAUSED 不在受众内(见 _STALLED_NONTERMINAL_STATUSES)。"""
    if str(getattr(duty_run, "status", "") or "") not in _STALLED_NONTERMINAL_STATUSES:
        return False
    now = time.time()
    cap = max(1, watch_response_cap_seconds(agent))
    if (now - lane_last_consumed_at(owner_home, lane)) <= _CONSUMPTION_FRESH_FACTOR * cap:
        return False  # 消费还新鲜=有人在岗(PENDING 刚起就在拉),不抢
    if _duty_thread_claim_running(agent, duty_run, now):
        return False  # 唤醒轮判读正在进行
    # 复活能拉起来的(未过 attempt/能力/channel 闸)交给 auto_start,别双驱;只接管【拉不起来】的。
    return not _orphan_revivable(duty_run)


def _orphan_revivable(duty_run: Any) -> bool:
    """这个非终态 run 是否还能被 auto_start_stalled_orphans 复活(同一把判据,避免双驱)。
    复用 capability_auto_sweep 的候选判定;判定不可用时保守 True(=可复活,本扫描不接管)。"""
    try:
        from .capability_auto_sweep import _is_stalled_dispatchable_orphan

        return bool(_is_stalled_dispatchable_orphan(duty_run))
    except Exception:
        return True


def _lane_still_manned(agent: Any, owner_home: Path, lane: dict[str, Any], duty_run: Any, status: str) -> bool:
    """终态 run ≠ 死岗:长跑盯守子代理的常态形态就是「turn 结束进 DONE、唤醒机制续驱
    下一轮判读」——按 run 状态一刀切会把正常判读中的岗当死岗反复换人(真机实锤:一个源
    respawn=3;churn 除了白烧 token,还把前任刚取走的在途批反复悬空)。三重结构化活性
    信号,命中任意一条即视为有人在岗、本轮不补:
    ①lane 消费新鲜:最近拉取/读游标推进 ≤ 2×自唤醒响应上限(正在有人消费);
    ②duty run 线程有 running claim:唤醒轮判读正在进行(长判读轮的中途);
    ③status=DONE 且 run 名下还有 enabled 循环提醒(排期在场=续驱会来)——但消费停摆
      超过 6×响应上限视为续驱僵死,照常补岗(防饿死;真死的 DONE 岗最迟这个窗被补)。
    判据全结构化(时间戳/claim 状态/policy 存在性);任何读取失败按"信号不在场"处理
    (回落旧行为=照常补岗,补岗路径永不因此断)。"""
    now = time.time()
    cap = max(1, watch_response_cap_seconds(agent))
    stale_for = now - lane_last_consumed_at(owner_home, lane)
    if stale_for <= _CONSUMPTION_FRESH_FACTOR * cap:
        return True
    if _duty_thread_claim_running(agent, duty_run, now):
        return True
    if status == TaskStatus.DONE.value and stale_for <= _DRIVEN_STALE_FACTOR * cap:
        return _has_enabled_policy_for(agent, str(getattr(duty_run, "id", "") or ""))
    return False


def _duty_thread_claim_running(agent: Any, duty_run: Any, now: float) -> bool:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "thread_for_task", None)):
        return False
    if not callable(getattr(store, "load_background_run_claim", None)):
        return False
    try:
        thread = store.thread_for_task(str(getattr(duty_run, "id", "") or ""))
        thread_id = str(getattr(thread, "thread_id", "") or "")
        if not thread_id:
            return False
        claim = store.load_background_run_claim(thread_id)
    except Exception:
        return False
    if not isinstance(claim, dict) or str(claim.get("status") or "") != "running":
        return False
    try:
        return float(claim.get("expires_at") or 0.0) > now
    except (TypeError, ValueError):
        return False


def _has_enabled_policy_for(agent: Any, run_id: str) -> bool:
    store = getattr(agent, "conversation_store", None)
    if store is None or not run_id or not callable(getattr(store, "list_progress_policies", None)):
        return False
    try:
        return any(
            str(getattr(policy, "task_id", "") or "") == run_id
            for policy in store.list_progress_policies(enabled_only=True)
        )
    except Exception:
        return False


def _lane_needs_watching(owner_home: Path, lane: dict[str, Any]) -> bool:
    """需要有人在岗的活跃盯守路(全结构化:closed/窗口时长/已流逝时间/未判积压)。
    窗口未走完是主场;窗口走完但 spool 还有已抬未判完的候选也算(P1 消费吞吐真机实锤:
    岗上 run 在窗口末尾 DONE/CANCELLED,留下的积压是窗口内的事件,判完才算盯完——
    原判据按 elapsed<window 一刀切,清账时段的死岗永远没人补,积压卡死 spool)。
    终点:积压清零(判完)或显式 close,不会永续补岗。

    /audit 保证档的无窗路(月级盯守常见形态)按【积压驱动】而非窗口驱动:契约是每条都判、
    判空才算完,判读员自报 DONE 但 spool 还压着未判候选时必须补岗续判(真机实锤:5 路无窗
    audit 判读员 DONE 后 respawn_count 恒 0、积压永久卡死——旧判据 window<=0 直接放弃这一
    整类,与 wake_backstop._lane_rebuild_eligible 已按积压驱动的口径不一致)。非 audit 的无窗
    尽力盯守(洪水直通、有损可接受)仍不归本扫描管:为 lossy-OK 的积压反复起判读子代理白烧
    模型,与两层限流的 triage 设计相悖。"""
    if bool(lane.get("closed")):
        return False
    window = int(lane.get("watch_window_seconds") or 0)
    if window <= 0:
        # 无窗:仅 /audit 保证档按积压驱动补岗(zero-drop 契约);非 audit 无窗不管。
        if bool(lane.get("audit_guarantee")):
            return lane_unjudged_backlog(owner_home, lane) > 0
        return False
    elapsed = time.time() - float(lane.get("opened_at") or 0.0)
    if elapsed < window:
        return True
    return lane_unjudged_backlog(owner_home, lane) > 0


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
    backlog = lane_unjudged_backlog(owner_home, lane)
    # 接管续的不只是游标,还有缓冲区:前任已抬升未判完的候选(含取走没确认的在途批)
    # 由 pull 最先交给继任者,这里把账写进接管指令(纯结构计数)。
    backlog_line = (
        f"注意缓冲区里还有 {backlog} 条前任已抬升、未确认判完的候选——接管后先 pull,"
        f"系统会把这批(含前任在途批)最先交给你,逐条重判上报完再续新流;"
        if backlog > 0
        else ""
    )
    if remaining > 0:
        situation = f"窗口未走完(剩余约 {remaining}s)"
        mission = "从持久化游标续盯到窗口结束(watch_stream action=open 同源即续)。"
    else:
        # 窗口走完的清账岗:任务只剩把已抬候选判完,别重开新窗(open 带
        # watch_window_seconds 会把已走完的窗重置成新一场,清账岗永远收不了工)。
        situation = "窗口已走完但缓冲区还有已抬升未判完的候选"
        mission = (
            "接管后 open 同源(不带 watch_window_seconds)再 pull,把积压逐条重判上报,"
            "确认命中的照常入账;积压清零后 close 收工。"
        )
    duty_status = str(getattr(duty_run, "status", "") or "")
    # 岗上 run 状态措辞:终态=已终态;非终态但卡死(拉不起来的 PENDING/PLANNING/BLOCKED)=停摆。
    duty_phrase = f"已终态({duty_status})" if duty_status in _DEAD_LANE_STATUSES else f"停摆无法复活({duty_status})"
    reason = (
        f"盯守补岗: watch {watch_id} {situation}而岗上 run "
        f"{getattr(duty_run, 'id', '')} {duty_phrase};"
        f"{backlog_line}"
        f"{mission}"
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
