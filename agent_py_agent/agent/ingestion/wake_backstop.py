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
from .watch_state import list_states, state_dir

_LOGGER = logging.getLogger(__name__)

# 只兜内部自唤醒的盯守类 policy(wait 工具/机制层监督登记的 kind);
# 外呼渠道的用户定期汇报 policy 是用户显式节奏,不属于盯守续航,绝不碰。
_WATCH_POLICY_KIND = "subagent_progress_watch"
_BACKSTOP_TOOL = "watch_backlog_backstop"
# 响应上限下限 = wait 工具允许的最短间隔(_MIN_SECONDS 同值):兜底不比模型能设的最短更激进。
_MIN_CAP_SECONDS = 60
_DEFAULT_CAP_SECONDS = 120
# 消费新鲜窗(×响应上限):最近一次拉取/读游标推进在这窗内 = 有人在消费,不重建排期。
# 与补岗扫描(watch_lane_sweep)的在岗否决同一把尺。
_CONSUMPTION_FRESH_FACTOR = 2


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
    """②排期自愈:活跃 backlog 路的消费者名下连 enabled 盯守 policy 都没了 + 消费已停
    + 线程无 running claim → 机制层重建兜底 policy。由 supervision 节奏召唤;绝不外抛。

    per-lane 判(P1 消费吞吐真机实锤):旧实现 owner 级短路——owner 还有任何一条 enabled
    盯守 policy(哪怕是别路流的)就整体不重建,多路多子代理编队里死掉一路(policy 随
    run 终态被退休)后,这一路的积压永远没有排期来消费。
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
    manned_tasks = _watch_policy_task_ids(store)
    cap = watch_response_cap_seconds(agent)
    actions: list[dict[str, object]] = []
    seen_pullers: set[str] = set()  # 同一消费者的多路流只重建一份(唤醒轮会 pull 所有路)
    for lane in list_states(owner_home):
        if not _lane_rebuild_eligible(owner_home, lane, now):
            continue
        # 消费者归属:最近拉取方,回落开启方——冷启动路(收割者抬了积压、判读工还没来得
        # 及第一次 pull 就死/重启)last_puller 为空,恰是最落后、最需要自愈的一类,不许
        # 因"还没人拉过"被排除(上一轮真机重启 3/5 的结构缺口之二)。
        puller = str(lane.get("last_puller_run_id") or "").strip() or str(lane.get("opened_by_run") or "").strip()
        if not puller or puller in seen_pullers:
            continue
        # 这一路的消费者链上已有 enabled 盯守 policy(①expedite 的场)→ 不重复建;
        # 消费还新鲜(有人正在拉)也不建——重建只兜「排期断了且没人在消费」的死路。
        if {puller, str(lane.get("opened_by_run") or "").strip()} & manned_tasks:
            continue
        if (now - lane_last_consumed_at(owner_home, lane)) <= _CONSUMPTION_FRESH_FACTOR * cap:
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
    # 未 close 的 backlog 路即可重建——按【谁有未判积压】驱动,不看窗口形态(上一轮真机
    # 重启只 3/5:落后源躺平的结构缺口之一就是旧判据要求"有窗",把无窗长守——/audit
    # 月级盯守的常见形态——整类排除在自愈之外;重启后落后源没人接管=慢性丢)。
    # 防无限重建不靠窗口一刀切:调用方已有三道闸(该路消费者链上有 enabled policy 不建/
    # 消费新鲜不建/线程有 running claim 不建)。终点:积压清零(判完)或显式 close。
    if bool(lane.get("closed")):
        return False
    return lane_unjudged_backlog(owner_home, lane) > 0


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
                f"盯守排期自愈: watch {lane.get('watch_id')} 还有已抬升未判完的候选,"
                "但这一路没有任何在排期的自唤醒提醒——机制层重建;醒来后继续 pull 消费并逐条判读上报。"
            ),
            "watch_run_id": puller,
        },
    }


# ---------------------------------------------------------------------------
# 结构信号读取(纯文件,不复活引擎、不碰 registry 锁)
# ---------------------------------------------------------------------------


def _watch_policy_task_ids(store: Any) -> set[str]:
    """enabled 盯守类 policy 覆盖的消费者 run 集合(task_id/watch_run_id 两个绑定位都认)。"""
    ids: set[str] = set()
    for policy in store.list_progress_policies(enabled_only=True):
        if _is_watch_policy(policy):
            ids.update(_policy_binding_ids(policy))
    return ids


def _policy_binding_ids(policy: Any) -> list[str]:
    values = (
        getattr(policy, "task_id", ""),
        (getattr(policy, "metadata", None) or {}).get("watch_run_id", ""),
    )
    return [text for value in values if (text := str(value or "").strip())]


def lane_last_consumed_at(owner_home: Path, lane: dict[str, Any]) -> float:
    """这路流最近一次被消费的时刻:watch 快照的 last_pull_at 与读游标 sidecar 的
    updated_at 取较新者(消费者可能在别的进程,只写 sidecar 不写快照)。
    排期自愈与补岗扫描(watch_lane_sweep)共用这一把尺。"""
    latest = float(lane.get("last_pull_at") or 0.0)
    watch_id = str(lane.get("watch_id") or "")
    if not watch_id:
        return latest
    report = read_json_object_report(
        state_dir(owner_home) / f"{watch_id}.read.json", context="watch_wake_backstop.consumed_at"
    )
    if report.load_error is not None:
        return latest
    try:
        return max(latest, float(report.payload.get("updated_at") or 0.0))
    except (TypeError, ValueError):
        return latest


def owner_has_incomplete_watch(agent: Any, *, now: float | None = None) -> bool:
    """owner 名下是否有「未 close 且(窗口未满 或 spool 还有未判完候选)」的盯守路。

    收口退休守卫(progress_policy_retirement)与调度器终态退休豁免共用的一把尺:
    积压是盯守期内的事件,判完才算盯完——唤醒链在这之前不许死。任何失败保守 False
    (照常退休,不改旧行为)。纯盘上结构信号(opened_at/window/closed/写入-ack 计数)。
    """
    owner_home = _owner_home(agent)
    if owner_home is None:
        return False
    return owner_home_has_incomplete_watch(owner_home, now=now)


def owner_home_has_incomplete_watch(owner_home: Path, *, now: float | None = None) -> bool:
    """同 owner_has_incomplete_watch,但直接吃 owner_home(磁盘级 owner 唤醒发现用:
    那边还没有 agent 实例)。任何失败保守 False。"""
    try:
        moment = now if now is not None else time.time()
        return any(_watch_row_incomplete(owner_home, row, moment) for row in list_states(owner_home))
    except Exception:
        return False


def _watch_row_incomplete(owner_home: Path, row: dict[str, Any], now: float) -> bool:
    if row.get("closed"):
        return False
    window = int(row.get("watch_window_seconds") or 0)
    if window > 0 and (now - float(row.get("opened_at") or 0.0)) < window:
        return True
    return lane_unjudged_backlog(owner_home, row) > 0


def is_watch_progress_policy(policy: Any) -> bool:
    """结构化辨认盯守续航类 policy(wait 登记/排期自愈重建/续推保底同一个 kind)。"""
    return _is_watch_policy(policy)


def stalled_unjudged_watch_lanes(agent: Any, *, now: float | None = None) -> list[dict[str, Any]]:
    """未 close、spool 还有未判完候选、且消费已停摆(>新鲜窗)的盯守路清单。

    主代理交付收口闸用:有人正在消费(新鲜窗内)的路不算——那是活岗,轮不到收口方管;
    只有「积压卡着没人拉」的路才该在收尾前被对账。任何失败保守返回空(不挡收口)。
    """
    owner_home = _owner_home(agent)
    if owner_home is None:
        return []
    try:
        moment = now if now is not None else time.time()
        fresh_window = _CONSUMPTION_FRESH_FACTOR * watch_response_cap_seconds(agent)
        rows = (_stalled_lane_row(owner_home, lane, moment, fresh_window) for lane in list_states(owner_home))
        return [row for row in rows if row is not None]
    except Exception:
        return []


def _stalled_lane_row(owner_home: Path, lane: dict[str, Any], moment: float, fresh_window: float) -> dict[str, Any] | None:
    if bool(lane.get("closed")):
        return None
    backlog = lane_unjudged_backlog(owner_home, lane)
    if backlog <= 0:
        return None
    if (moment - lane_last_consumed_at(owner_home, lane)) <= fresh_window:
        return None
    return {
        "watch_id": str(lane.get("watch_id") or ""),
        "source_url": str(lane.get("source_url") or ""),
        "unjudged_candidates": backlog,
    }


def run_unjudged_watch_backlog(agent: Any, run_id: str) -> int:
    """这个 run 名下(它是最近消费者或开启者)未 close 盯守路的未判完候选合计。

    子代理收口闸用的一把尺:自己 spool 里还有已抬升未判完的候选 = 活没干完,
    不该体面收口(P1 真机实锤:子代理 DONE/CANCELLED 停了,把待判积压留在缓冲区)。
    任何失败保守返回 0(不挡收口,旧行为)。
    """
    text = str(run_id or "").strip()
    owner_home = _owner_home(agent)
    if not text or owner_home is None:
        return 0
    try:
        return sum(_lane_backlog_held_by(owner_home, lane, text) for lane in list_states(owner_home))
    except Exception:
        return 0


def _lane_backlog_held_by(owner_home: Path, lane: dict[str, Any], run_id: str) -> int:
    if bool(lane.get("closed")):
        return 0
    holders = {str(lane.get("last_puller_run_id") or "").strip(), str(lane.get("opened_by_run") or "").strip()}
    if run_id not in holders:
        return 0
    return lane_unjudged_backlog(owner_home, lane)


def run_judged_watch_count(agent: Any, run_id: str) -> int:
    """这个 run 名下未 close 盯守路的【已判(acked)】候选合计——子代理续判门的进展信号。

    续判门(final_exit)判「判读员是不是真在判」不能看积压降没降:入流可能比判得快,
    积压照涨但判读员一直在判(overload 非卡死)。看 acked 在不在涨才准:涨=有进展继续按住,
    连续几轮不涨=判读卡死/模型拒判,放行退出交给补岗兜底。任何失败保守返回 0。
    """
    text = str(run_id or "").strip()
    owner_home = _owner_home(agent)
    if not text or owner_home is None:
        return 0
    try:
        return sum(_lane_acked_held_by(owner_home, lane, text) for lane in list_states(owner_home))
    except Exception:
        return 0


def _lane_acked_held_by(owner_home: Path, lane: dict[str, Any], run_id: str) -> int:
    if bool(lane.get("closed")):
        return 0
    holders = {str(lane.get("last_puller_run_id") or "").strip(), str(lane.get("opened_by_run") or "").strip()}
    if run_id not in holders:
        return 0
    return _consumed_candidates(owner_home, str(lane.get("watch_id") or ""))


def _has_active_backlog(owner_home: Path, now: float) -> bool:
    return any(lane_unjudged_backlog(owner_home, lane) > 0 for lane in list_states(owner_home) if _lane_active(lane, now))


def _lane_active(lane: dict[str, Any], now: float) -> bool:
    """未关闭的盯守路都算活跃(g8 不足4·窗口末尾清账:调用方叠加 backlog>0 才动作——
    已捞进 spool 的候选是窗口内的事件,窗口走完也必须判完才算盯完;按窗口切活跃会让
    末尾积压无人来判=静默丢弃。真机窗口到期剩 200/97 条已抬候选弃判即此漏)。
    终点两条都是结构信号:积压清零(判完)或模型显式 close。"""
    return not bool(lane.get("closed"))


def lane_unjudged_backlog(owner_home: Path, lane: dict[str, Any]) -> int:
    """一条盯守路已抬进 spool 而未被消费判读的候选数(纯盘上结构信号:引擎累计写入数 −
    读游标累计消费数)。唤醒兜底/收口退休守卫共用这一把尺。"""
    written = int((lane.get("totals") or {}).get("spool_candidates") or 0)
    if written <= 0:
        return 0
    return max(0, written - _consumed_candidates(owner_home, str(lane.get("watch_id") or "")))


def _consumed_candidates(owner_home: Path, watch_id: str) -> int:
    """未判积压的"已处理"半边取 ack 口径(交付≠判完):交付出去、消费者死在判读中途
    还没确认的在途批仍算未判——接管/唤醒兜底都不能把它们从账上抹掉。旧 sidecar 无 acked
    字段时回落已交付数(历史口径,acked_candidates 内置该回落)。"""
    if not watch_id:
        return 0
    from .harvester import consumed_and_acked_on_disk

    _consumed, acked = consumed_and_acked_on_disk(owner_home, watch_id)
    return acked


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
    "is_watch_progress_policy",
    "lane_last_consumed_at",
    "lane_unjudged_backlog",
    "owner_has_incomplete_watch",
    "owner_home_has_incomplete_watch",
    "rebuild_missing_watch_policies",
    "run_judged_watch_count",
    "run_unjudged_watch_backlog",
    "stalled_unjudged_watch_lanes",
    "watch_response_cap_seconds",
]
