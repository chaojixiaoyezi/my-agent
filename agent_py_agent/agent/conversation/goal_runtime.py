# LLM: Goal 只通过此处发布 canonical 去重唤醒；调用方先核对 active 目标与宿主停止边界。
# 模块用途: 将持续目标的精确身份写入已有 wake 队列，不创建另一套定时轮询或任务状态。
from __future__ import annotations

from .goal_progress_fuse import WAKE_SNAPSHOT_KEY, goal_progress_snapshot

"""Single wake authority for continuing one persisted thread goal."""


# LLM: 写入唯一目标 wake，去重键只来自持久 goal ID；正文是给模型的说明，不决定运行状态。
# 函数用途: 为已经获准继续的目标安排下一轮，重复发布不会创建多个待办。
def raise_goal_continuation_wake(
    store: object,
    goal: object,
    *,
    channel: str = "",
    conversation_id: str = "",
    now: float | None = None,
) -> object:
    task_status = ""
    try:
        link = store.tasks.load(str(getattr(goal, "task_id", "") or ""))
        task_status = str(getattr(link, "status", "") or "")
    except Exception:  # noqa: BLE001 - missing baseline is treated as progress, never as an idle slice.
        pass
    request: dict[str, object] = {
        "thread_id": str(getattr(goal, "thread_id", "") or ""),
        "root_task_id": str(getattr(goal, "task_id", "") or ""),
        "urgency": "normal",
        "reason": "thread_goal_continue",
        "summary": "Continue working toward the active thread goal.",
        "dedupe_key": f"thread-goal:{str(getattr(goal, 'goal_id', '') or '')}",
        "metadata": {
            "goal_id": str(getattr(goal, "goal_id", "") or ""),
            "channel": str(channel or ""),
            "conversation_id": str(conversation_id or ""),
            WAKE_SNAPSHOT_KEY: goal_progress_snapshot(goal, task_status),
        },
    }
    if now is not None:
        request["now"] = now
    return store.wakes.raise_signal(request)


__all__ = ["raise_goal_continuation_wake"]
