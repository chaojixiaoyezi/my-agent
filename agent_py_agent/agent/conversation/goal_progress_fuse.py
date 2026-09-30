# LLM: 只用每片工具账、Goal 内容 revision 与任务状态判断进展；不读取回复正文。计数保存在 Goal metadata，避免进程重启丢失。
# 模块用途: 为 Goal 自动续跑提供可持久恢复、结构化判据和复用宿主提示通道的无进展熔断。
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import ThreadGoal
    from .store_goals import GoalStore

FUSE_METADATA_KEY = "goal_continuation_fuse"
WAKE_SNAPSHOT_KEY = "goal_progress_snapshot"
NO_PROGRESS_REASON_CODE = "GOAL_CONTINUATION_NO_PROGRESS"
NO_PROGRESS_NOTICE_SOURCE = "goal_continuation"

_LOGGER = logging.getLogger("agent.conversation.goal_progress_fuse")


# LLM: 任一工具调用或结构化状态变化都重置；0 明确关闭熔断且不累加陈旧计数。
# 函数用途: 返回当前空片后的连续无进展数，以及是否达到正数上限。
def next_idle_slice_count(previous: int, progressed: bool, limit: int) -> tuple[int, bool]:
    if progressed or limit <= 0:
        return 0, False
    count = max(0, int(previous)) + 1
    return count, count >= limit


# LLM: 仅精确 active Goal 能消费同一 wake 一次；原子落账并在熔断时沿既有 paused 状态结算时钟。
# 函数用途: 持久累计目标空片数，达到阈值时停止自动续跑。
def record_goal_continuation_fuse(
    store: GoalStore, request: dict[str, Any]
) -> tuple[ThreadGoal | None, bool]:
    from ..gateway_parts.io import update_json_file_atomic
    from ..runtime_errors import DataCorruptionError
    from .store_goals import (
        _goal_collection_payload,
        _goals_from_update_data,
        _select_thread_goal,
    )
    from .store_io import now

    thread_id = str(request.get("thread_id") or "").strip()
    goal_id = str(request.get("goal_id") or "").strip()
    task_id = str(request.get("task_id") or "").strip()
    wake_id = str(request.get("wake_signal_id") or "").strip()
    if not wake_id:
        return None, False
    store._require_thread(thread_id)
    limit = max(0, int(request.get("idle_limit") or 0))
    progressed = bool(request.get("progressed"))
    current_time = now(request.get("now"))
    path = store.storage.goal_path(thread_id)
    updated_goal: ThreadGoal | None = None
    tripped = False

    # LLM: 在唯一目标集合事务内读取、去重和写入；revision 只在真实暂停时增长。
    # 函数用途: 原子记下一个 wake 对应的续跑结论。
    def updater(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated_goal, tripped
        goals, error = _goals_from_update_data(data, path=path)
        if error is not None:
            raise DataCorruptionError(str(error.get("message") or "conversation goal read failed"))
        current = _select_thread_goal(goals, goal_id=goal_id)
        if current is None or current.status != "active" or current.task_id != task_id:
            return data
        next_state, tripped = _next_fuse_state(current, wake_id, progressed, limit)
        if next_state is None:
            updated_goal = current
            return data
        updated_goal = _goal_with_fuse(store, current, next_state, current_time)
        return _goal_collection_payload(
            [updated_goal if goal.goal_id == current.goal_id else goal for goal in goals]
        )

    update_json_file_atomic(path, updater, require_existing=True)
    if tripped and updated_goal is not None:
        store.clock.clear(thread_id, goal_id=goal_id)
    return updated_goal, tripped


# LLM: 纯计算，不读写盘：同一 wake 已记过账返回 (None, False)，调用方不重复计数；否则给出新的 fuse 子对象
#   （连续空片数、最近 16 个已处理 wake、原因码）与是否熔断。
# 函数用途: 按上一次计数和本片有无进展，算出新的熔断状态。
def _next_fuse_state(
    current: ThreadGoal, wake_id: str, progressed: bool, limit: int
) -> tuple[dict[str, Any] | None, bool]:
    raw_state = current.metadata.get(FUSE_METADATA_KEY)
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    processed = state.get("processed_wake_ids")
    processed_ids = [str(value) for value in processed if str(value)] if isinstance(processed, list) else []
    if wake_id in processed_ids:
        return None, False
    try:
        previous = int(state.get("idle_slices", 0) or 0)
    except (TypeError, ValueError):
        previous = 0
    idle_slices, tripped = next_idle_slice_count(previous, progressed, limit)
    return {
        "idle_slices": idle_slices,
        "processed_wake_ids": [*processed_ids, wake_id][-16:],
        "reason_code": NO_PROGRESS_REASON_CODE if tripped else "",
    }, tripped


# LLM: 未熔断只替换 fuse 子对象；熔断（原因码为 NO_PROGRESS_REASON_CODE）时沿既有 paused 状态结算活跃时长、
#   更新时间并推进内容 revision。副作用：熔断时从共享时钟取走本段已用时长。
# 函数用途: 生成写回目标集合的新目标记录。
def _goal_with_fuse(
    store: GoalStore, current: ThreadGoal, next_state: dict[str, Any], current_time: float
) -> ThreadGoal:
    from dataclasses import replace

    metadata = {**current.metadata, FUSE_METADATA_KEY: next_state}
    if next_state.get("reason_code") != NO_PROGRESS_REASON_CODE:
        return replace(current, metadata=metadata)
    return replace(
        current,
        status="paused",
        time_used_seconds=current.time_used_seconds + store.clock.take_elapsed_seconds(current),
        updated_at=current_time,
        revision=current.revision + 1,
        metadata=metadata,
    )


# LLM: 用户消息和恢复只修改 fuse metadata，不改目标正文、内容版本或执行状态。
# 函数用途: 原子清零连续空片数，并按调用方意图保留或清除暂停原因。
def reset_goal_continuation_fuse(
    store: GoalStore, request: dict[str, Any]
) -> ThreadGoal | None:
    from dataclasses import replace

    from ..gateway_parts.io import update_json_file_atomic
    from ..runtime_errors import DataCorruptionError
    from .store_goals import (
        _goal_collection_payload,
        _goals_from_update_data,
        _select_thread_goal,
    )

    thread_id = str(request.get("thread_id") or "").strip()
    goal_id = str(request.get("goal_id") or "").strip()
    clear_reason = request.get("clear_reason") is True
    store._require_thread(thread_id)
    path = store.storage.goal_path(thread_id)
    updated_goal: ThreadGoal | None = None

    # LLM: 保留 fuse 子对象与 Goal 其它 metadata；无变化时不写盘。
    # 函数用途: 按精确 Goal ID 重置持续续跑熔断计数。
    def updater(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated_goal
        goals, error = _goals_from_update_data(data, path=path)
        if error is not None:
            raise DataCorruptionError(str(error.get("message") or "conversation goal read failed"))
        current = _select_thread_goal(goals, goal_id=goal_id)
        if current is None:
            return data
        raw_state = current.metadata.get(FUSE_METADATA_KEY)
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        next_state = {**state, "idle_slices": 0}
        if clear_reason or current.status == "active":
            next_state["reason_code"] = ""
        metadata = {**current.metadata, FUSE_METADATA_KEY: next_state}
        updated_goal = replace(current, metadata=metadata)
        if metadata == current.metadata:
            return data
        return _goal_collection_payload(
            [updated_goal if goal.goal_id == current.goal_id else goal for goal in goals]
        )

    update_json_file_atomic(path, updater, require_existing=True)
    return updated_goal


# LLM: 快照只含可比较的宿主字段；Goal revision 不随 token/时间记账变化，避免把用量刷新误判成业务进展。
# 函数用途: 为一次自动续跑记录 Goal 与任务状态基线，不包含用户或模型正文。
def goal_progress_snapshot(goal: object, task_status: str) -> dict[str, object]:
    return {
        "goal_id": str(getattr(goal, "goal_id", "") or ""),
        "goal_revision": int(getattr(goal, "revision", 0) or 0),
        "goal_status": str(getattr(goal, "status", "") or ""),
        "task_id": str(getattr(goal, "task_id", "") or ""),
        "task_status": str(task_status or ""),
    }


# LLM: 缺失/不完整的旧 wake 基线按“有进展”处理，先建立可信下一片边界，不因迁移数据误触发暂停。
# 函数用途: 只比较工具计数及 Goal/任务结构字段，判断本片是否应清空空转计数。
def slice_has_progress(signal: object, report: object, goal: object, task_status: str) -> bool:
    if int(getattr(report, "tool_call_count", 0) or 0) > 0:
        return True
    if int(getattr(report, "material_progress_count", 0) or 0) > 0:
        return True
    metadata = getattr(signal, "metadata", {})
    baseline = metadata.get(WAKE_SNAPSHOT_KEY) if isinstance(metadata, Mapping) else None
    current = goal_progress_snapshot(goal, task_status)
    if not isinstance(baseline, Mapping) or any(not baseline.get(key) for key in current):
        return True
    return dict(baseline) != current


# LLM: 输入新用户消息或显式恢复只清内部计数；被熔断暂停时保留暂停原因，恢复动作才清除旧原因。
# 函数用途: 通过 GoalStore 原子重置计数，不自行改变目标的 active/paused 生命周期。
def reset_goal_progress_fuse(store: object, thread_id: str, *, clear_reason: bool = False) -> bool:
    goals = getattr(store, "goals", None)
    normalized = str(thread_id or "").strip()
    if goals is None or not normalized:
        return False
    try:
        goal = goals.load(normalized)
        if goal is None:
            return False
        return goals.reset_continuation_fuse(
            {"thread_id": normalized, "goal_id": goal.goal_id, "clear_reason": clear_reason}
        ) is not None
    except Exception:  # noqa: BLE001 - 重置诊断不能丢用户消息或恢复请求。
        _LOGGER.warning("goal continuation fuse reset failed(thread=%s)", normalized, exc_info=True)
        return False


# LLM: 通知正文由宿主固定生成，结构原因码随 notice 保存；复用 TUI/IM 同一最终消息投递口并按来源/代码替换去重。
# 函数用途: 把熔断结果排入会话的待送达宿主提示，不另建消息或推送通道。
def queue_goal_no_progress_notice(store: object, goal: object) -> bool:
    from .host_notices import host_notice, queue_host_notice

    thread_id = str(getattr(goal, "thread_id", "") or "")
    goal_id = str(getattr(goal, "goal_id", "") or "")
    goals = getattr(store, "goals", None)
    if goals is None or not thread_id or not goal_id:
        return False
    with goals.transition_guard(thread_id):
        current = goals.load(thread_id, goal_id=goal_id)
        current_metadata = getattr(current, "metadata", {})
        current_state = (
            current_metadata.get(FUSE_METADATA_KEY)
            if isinstance(current_metadata, Mapping)
            else {}
        )
        if (
            current is None
            or current.status != "paused"
            or not isinstance(current_state, Mapping)
            or current_state.get("reason_code") != NO_PROGRESS_REASON_CODE
        ):
            return False
        current_count = int(current_state.get("idle_slices", 0) or 0)
        notice = host_notice(
            NO_PROGRESS_NOTICE_SOURCE,
            NO_PROGRESS_REASON_CODE,
            f"持续目标连续 {current_count} 片没有工具调用或目标/任务状态变化，自动续跑已暂停。请检查进度后用 /goal resume 恢复。",
            details={"goal_id": goal_id, "reason_code": NO_PROGRESS_REASON_CODE},
        )
        return queue_host_notice(store, thread_id, notice, replace_same_code=True)


__all__ = [
    "FUSE_METADATA_KEY",
    "NO_PROGRESS_NOTICE_SOURCE",
    "NO_PROGRESS_REASON_CODE",
    "WAKE_SNAPSHOT_KEY",
    "goal_progress_snapshot",
    "next_idle_slice_count",
    "queue_goal_no_progress_notice",
    "record_goal_continuation_fuse",
    "reset_goal_continuation_fuse",
    "reset_goal_progress_fuse",
    "slice_has_progress",
]
