# LLM: 交付收口(report.ok=True)→ 自动退休本任务的循环提醒。契约:wait 登记的 progress
#   policy 按 interval 无限复发(mark_progress_reported 每次顺延 next_due),而主任务的会话
#   task link 只有子代理完成路径(runner_completion_wake)会置终态——主代理自己收口后,这些
#   提醒会永远每个间隔唤醒一次 LLM 空转(churn)。这里挂在 closeout 落盘的唯一 choke point
#   (sync_run_task_workspace_closeout)上,按结构化事实(report.ok)收口;best-effort,任何
#   失败静默记日志、绝不影响交付本身。改动时同步 tests/test_wait_tool_self_wake.py。
# 模块用途: 任务验收通过后,把它名下还在循环的 wait 提醒自动停掉,别让完结任务继续闹钟。
from __future__ import annotations

import logging

_LOGGER = logging.getLogger("agent.runtime.progress_policy_retirement")


# 函数用途: closeout 落盘时调用——清单还有未闭环项、任务名下却没有任何 enabled 循环提醒
#   → 机制层补登一个续推提醒(g8 问题B·solo 保底:纯 solo 从没派过子代理时,唤醒轮消费侧的
#   补登永不触发,主 run 一收口续推链就无从建立,大工程如实停在半截)。与唤醒轮补登
#   (conversation.runtime._ensure_open_coverage_wake_chain)同构同 shape;登记后的生命周期
#   完全复用既有机制:无进展退避 2^streak 封顶、清单全闭后收口自动退休(下方 retire 函数)、
#   任务终态由调度器退休——三条既有终点都在,不会永不收口。判据全结构(清单计数/policy
#   存在性/线程可解析);best-effort,失败只记日志绝不影响交付。
def ensure_open_coverage_continuation(agent, params) -> None:
    if str(getattr(params, "context_scope", "") or "default").strip().lower() not in {"", "default"}:
        return  # 子代理 runner(task_local)/内部轮(control_plane)不替根任务立提醒
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "set_progress_policy", None)):
        return
    task_id = str(getattr(params, "task_id", "") or "").strip()
    if not task_id:
        return
    try:
        if _open_coverage_target_count(agent, params) <= 0:
            return
        if any(
            str(getattr(policy, "task_id", "") or "") == task_id
            for policy in store.list_progress_policies(enabled_only=True)
        ):
            return
        thread = store.thread_for_task(task_id)
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
        if not thread_id:
            return  # 无会话线程(单趟 cli 等)诚实跳过,不瞎建
        interval = int(getattr(getattr(agent, "config", None), "dispatch_supervision_reminder_seconds", 0) or 0)
        store.set_progress_policy(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "interval_seconds": max(60, interval) if interval > 0 else 180,
                "route_channel": "internal",
                "route_target": "",
                "metadata": {
                    "kind": "subagent_progress_watch",
                    "tool": "coverage_open_continuation",
                    "scope": "own_task_tree",
                    "reason": "机制层续推保底:任务清单还有未闭环项,到点继续推进剩余项(续派或自己做),全部闭环并收口后自动停止",
                    "watch_run_id": task_id,
                },
            }
        )
        _LOGGER.info("open-coverage continuation policy ensured task=%s", task_id)
    except Exception:
        _LOGGER.warning("open-coverage continuation ensure failed task=%s", task_id, exc_info=True)


# 函数用途: closeout 落盘时调用——ok=True 才动手,把绑定本任务的 enabled 循环提醒全部禁用。
def retire_task_progress_policies_on_closeout(agent, params, report) -> None:
    if not isinstance(report, dict) or report.get("ok") is not True:
        return
    # 只有任务当事人(default scope 的主代理轮)收口才退休提醒。子代理 runner
    # (task_local)/内部轮(control_plane)的 task_id 就是根任务 id——编队里第一个
    # 子代理干净收口就会把主任务的监督提醒全体退休,后面几路就没人巡场了。
    if str(getattr(params, "context_scope", "") or "default").strip().lower() not in {"", "default"}:
        return
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "list_progress_policies", None)):
        return
    task_id = str(getattr(params, "task_id", "") or "").strip()
    if not task_id:
        return
    # 不足3守卫(与下面 watch 窗口守卫同构):coverage 清单还有未闭环项时整体不退——ok=True
    # 只说明本轮交付过门(coverage 是软门),账没对完唤醒链就不该断,否则再没有未来轮次把
    # 剩余项推下去(真机 u-fixtest2:唤醒轮收口退休监督提醒→任务停 8/24)。清单全闭后的
    # 收口照常退休。纯结构判据(清单计数);读账失败保守按 0(照常退休,不改旧行为)。
    if _open_coverage_target_count(agent, params) > 0:
        _LOGGER.info("progress policy retirement skipped: open coverage targets remain task=%s", task_id)
        return
    try:
        _disable_policies_for_task(store, task_id, _incomplete_watch_open(agent))
    except Exception:
        _LOGGER.warning("progress policy retirement failed task=%s", task_id, exc_info=True)


# 函数用途: 主账本(closeout 账本键同尺)coverage 清单未闭环项计数;任何失败保守返回 0。
def _open_coverage_target_count(agent, params) -> int:
    try:
        from types import SimpleNamespace

        from ...task_progress import read_task_progress
        from ..delivery_closeout.task_progress_gate import closeout_ledger_run_id
        from .owner_roots import runtime_owner_root

        run_id = closeout_ledger_run_id(SimpleNamespace(agent=agent, params=params))
        if not run_id:
            return 0
        progress = read_task_progress(runtime_owner_root(agent), run_id)
        coverage = progress.get("coverage") if isinstance(progress, dict) else None
        counts = coverage.get("counts") if isinstance(coverage, dict) else None
        if not isinstance(counts, dict):
            return 0
        return max(0, int(counts.get("targets_incomplete") or 0))
    except Exception:
        return 0


def _disable_policies_for_task(store, task_id: str, incomplete_watch_open: bool) -> None:
    for policy in store.list_progress_policies(enabled_only=True):
        if str(getattr(policy, "task_id", "") or "") != task_id:
            continue
        # 有未完成的开着的盯守时,不退休 watch 登记的循环提醒:模型可能在盯守窗口未满时
        # 就 ok=True 收口(判读层提前收工),但盯守窗口没走完唤醒链就不该断,否则盯守睡死、
        # 之后写入的目标全漏(§1 真机实锤)。纯结构化判定:policy 由 wait 登记 + owner 名下
        # 有窗口未满且未 close 的 watch。非 watch 提醒(dispatch 监督等)照常退休。
        if incomplete_watch_open and _is_watch_policy(policy):
            continue
        store.disable_progress_policy(policy.policy_id)


# 函数用途: wait 工具登记盯守提醒时会在 metadata 打 watch_run_id 标;据此结构化辨认 watch 提醒。
def _is_watch_policy(policy) -> bool:
    metadata = getattr(policy, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return bool(str(metadata.get("watch_run_id") or "").strip())


# 函数用途: owner 名下是否有「未 close 且(窗口未满 或 spool 还有未判积压)」的盯守。
#   判定本体在 wake_backstop.owner_has_incomplete_watch(与调度器终态退休豁免同一把尺:
#   g8 不足4·窗口末尾清账——积压是窗口内的事件,判完才算盯完,唤醒链在这之前不许死)。
def _incomplete_watch_open(agent) -> bool:
    try:
        from ...ingestion.wake_backstop import owner_has_incomplete_watch

        return owner_has_incomplete_watch(agent)
    except Exception:
        return False


__all__ = ["ensure_open_coverage_continuation", "retire_task_progress_policies_on_closeout"]
