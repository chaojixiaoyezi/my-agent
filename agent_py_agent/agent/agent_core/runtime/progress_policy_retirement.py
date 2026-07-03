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
        _disable_policies_for_task(store, task_id)
    except Exception:
        _LOGGER.warning("progress policy retirement failed task=%s", task_id, exc_info=True)


def _disable_policies_for_task(store, task_id: str) -> None:
    for policy in store.list_progress_policies(enabled_only=True):
        if str(getattr(policy, "task_id", "") or "") == task_id:
            store.disable_progress_policy(policy.policy_id)


__all__ = ["retire_task_progress_policies_on_closeout"]
