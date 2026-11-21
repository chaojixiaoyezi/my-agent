# LLM: Goal 只通过此处发布 canonical 去重唤醒；调用方先核对 active 目标与宿主停止边界。
# 模块用途: 将持续目标的精确身份写入已有 wake 队列，不创建另一套定时轮询或任务状态。
from __future__ import annotations

from .goal_progress_fuse import WAKE_SNAPSHOT_KEY, frozen_goal_disposition, goal_progress_snapshot

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
    goal_meta = getattr(goal, "metadata", None)
    metadata: dict[str, object] = {
        "goal_id": str(getattr(goal, "goal_id", "") or ""),
        "channel": str(channel or ""),
        "conversation_id": str(conversation_id or ""),
        WAKE_SNAPSHOT_KEY: goal_progress_snapshot(goal, task_status),
    }
    if isinstance(goal_meta, dict):
        if goal_meta.get("plugin_origin"):
            metadata["plugin_origin"] = goal_meta["plugin_origin"]
            metadata["tool_policy"] = "none"
        elif goal_meta.get("tool_policy") == "none":
            metadata["tool_policy"] = "none"
    request: dict[str, object] = {
        "thread_id": str(getattr(goal, "thread_id", "") or ""),
        "root_task_id": str(getattr(goal, "task_id", "") or ""),
        "urgency": "normal",
        "reason": "thread_goal_continue",
        "summary": "Continue working toward the active thread goal.",
        "dedupe_key": f"thread-goal:{str(getattr(goal, 'goal_id', '') or '')}",
        "metadata": metadata,
    }
    if now is not None:
        request["now"] = now
    storage = getattr(store, "storage", None)
    if storage is not None:
        from .store_wake_publication import published_signal

        # 同键 pending 若已被本目标记账（该片裁决已冻结），它只剩原处理者确认，不能再吸收新的续跑请求；
        # 显式接替为新一代，锁内仍核对它就是当前那条 pending。
        current = published_signal(storage, request["thread_id"], request["dedupe_key"])
        if current is not None and current.status == "pending" and frozen_goal_disposition(
                goal, current.wake_signal_id) is not None:
            request["supersedes"] = current.wake_signal_id
    return store.wakes.raise_signal(request)


__all__ = ["raise_goal_continuation_wake"]
