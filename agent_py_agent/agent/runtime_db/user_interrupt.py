# LLM: “用户中断了这一轮”的唯一结构化事实：Esc / /interrupt 时给被中断的主执行代次追加一条 runtime_events。
#   账本自愈（owner_wake_discovery._filter_by_runtime_authority）只按这条事件区分“用户中断、可续接”与孤儿回收等
#   真正的取消；不读消息正文，不看用户发的是不是“继续”。事件绑定精确 attempt：用户再发消息开出新代次后自然失效。
#   改事件名、窗口或判定口径时，要同步 control_service._record_user_interrupt、cli 本地入口与
#   test_user_interrupt_resume.py。
# 模块用途: 记录用户中断，并算出被中断的任务在多久之内仍保持可续接。
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

USER_INTERRUPT_EVENT = "agent_run.user_interrupted"
# 用户中断后保持可续接的时长（秒）：期间账本自愈不把任务收成 cancelled，用户再发消息就接着原任务；
# 过了这个时长仍没人回来，自愈照原规则把它收成 cancelled（原因 user_interrupt_expired）。
USER_INTERRUPT_RESUME_SECONDS = 24 * 60 * 60


# LLM: 优先用调用方已发布的精确执行身份（本地入口的 LocalRunControl 绑定）；没有时按任务取主执行轮当前代次
#   （Gateway 入口）。查不到主执行轮就不记，返回 False，自愈退回旧行为。副作用：追加一条 runtime_events。
# 函数用途: 在用户中断一轮时，给这次执行代次记一条“用户中断”事件。
def record_user_interrupt(
    repo: Any, task_id: str, *, source: str, binding: Mapping[str, str] | None = None,
) -> bool:
    agent_run_id = str((binding or {}).get("agent_run_id") or "").strip()
    attempt_id = str((binding or {}).get("attempt_id") or "").strip()
    task_run_id = ""
    if not (agent_run_id and attempt_id):
        row = repo.main_agent_run_for_task(task_id)
        if row is None:
            return False
        agent_run_id = str(row["agent_run_id"] or "")
        attempt_id = str(row["current_attempt_id"] or "")
        task_run_id = str(row["task_run_id"] or "")
    if not (agent_run_id and attempt_id):
        return False
    repo.append_event(
        event_type=USER_INTERRUPT_EVENT,
        attempt_id=attempt_id,
        agent_run_id=agent_run_id,
        task_run_id=task_run_id,
        payload={"task_id": task_id, "source": source, "resume_window_seconds": USER_INTERRUPT_RESUME_SECONDS},
    )
    return True


# LLM: 只认与主执行轮当前代次（current_attempt_id）相同的用户中断事件；取最近一条的写入时刻加窗口。
#   没有这类事件返回 0.0（调用方按旧规则处理）。只读 runtime_events。
# 函数用途: 返回被用户中断的这次执行在什么时刻之前仍可续接；不是用户中断的返回 0。
def user_interrupt_resume_until(repo: Any, run_row: Any) -> float:
    attempt_id = str(run_row["current_attempt_id"] or "")
    if not attempt_id:
        return 0.0
    events = repo.events_for_agent_run(str(run_row["agent_run_id"] or ""), event_type=USER_INTERRUPT_EVENT, limit=20)
    stamps = [float(event.get("created_at") or 0.0) for event in events if event.get("attempt_id") == attempt_id]
    return max(stamps) + USER_INTERRUPT_RESUME_SECONDS if stamps else 0.0
