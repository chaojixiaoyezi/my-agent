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
    try:
        _disable_policies_for_task(store, task_id, _incomplete_watch_open(agent))
    except Exception:
        _LOGGER.warning("progress policy retirement failed task=%s", task_id, exc_info=True)


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


# 函数用途: owner 名下是否有「窗口未满且未 close」的盯守(纯结构化:opened_at+window+closed)。
#   零耦合判读:只读 watch 快照的时间戳/布尔标。任何失败保守返回 False(照常退休,不改旧行为)。
def _incomplete_watch_open(agent) -> bool:
    owner_home = str(getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or "").strip()
    if not owner_home:
        return False
    try:
        import time
        from pathlib import Path

        from ...ingestion.watch_state import list_states

        now = time.time()
        return any(_watch_row_incomplete(row, now) for row in list_states(Path(owner_home)))
    except Exception:
        return False


# 函数用途: 单条 watch 快照是否「未 close 且窗口未满」(无窗守望不受此保护,靠 idle 自停/显式 close)。
def _watch_row_incomplete(row: dict, now: float) -> bool:
    if row.get("closed"):
        return False
    window = int(row.get("watch_window_seconds") or 0)
    if window <= 0:
        return False
    return (now - float(row.get("opened_at") or 0.0)) < window


__all__ = ["retire_task_progress_policies_on_closeout"]
