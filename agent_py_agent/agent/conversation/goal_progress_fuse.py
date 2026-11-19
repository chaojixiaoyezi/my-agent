# LLM: 只用每片工具账、Goal 内容 revision 与任务状态判断进展；不读取回复正文。计数保存在 Goal metadata，避免进程重启丢失。
# 模块用途: 为 Goal 自动续跑提供可持久恢复、结构化判据和复用宿主提示通道的无进展熔断。
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import ThreadGoal
    from .store_goals import GoalStore

FUSE_METADATA_KEY = "goal_continuation_fuse"
WAKE_SNAPSHOT_KEY = "goal_progress_snapshot"
NO_PROGRESS_REASON_CODE = "GOAL_CONTINUATION_NO_PROGRESS"
NO_PROGRESS_NOTICE_SOURCE = "goal_continuation"
# 续跑资格被本片结束原因扣下（Goal 仍 active 但不会再有下一片）时的宿主提示代码，与熔断提示同来源、按代码各留一条。
WITHHELD_REASON_CODE = "GOAL_CONTINUATION_WITHHELD"

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

    from .goal_transition import goal_transition_event

    metadata = {**current.metadata, FUSE_METADATA_KEY: next_state}
    if next_state.get("reason_code") != NO_PROGRESS_REASON_CODE:
        return replace(current, metadata=metadata)
    return replace(
        current,
        status="paused",
        time_used_seconds=current.time_used_seconds + store.clock.take_elapsed_seconds(current),
        updated_at=current_time,
        revision=current.revision + 1,
        transition=goal_transition_event(current, status="paused", revision=current.revision + 1,
                                         now=current_time, source="no_progress_fuse"),
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
#   只认成功的工具调用（ok 为真，含只读成功）：全部失败的片不算进展，否则同一失败可以无限续跑且永不熔断。
# 函数用途: 只比较成功工具计数及 Goal/任务结构字段，判断本片是否应清空空转计数。
def slice_has_progress(signal: object, report: object, goal: object, task_status: str) -> bool:
    if int(getattr(report, "tool_success_count", 0) or 0) > 0:
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
    from .host_notices import queue_host_notice

    thread_id = str(getattr(goal, "thread_id", "") or "")
    goal_id = str(getattr(goal, "goal_id", "") or "")
    goals = getattr(store, "goals", None)
    if goals is None or not thread_id or not goal_id:
        return False
    with goals.transition_guard(thread_id):
        current = goals.load(thread_id, goal_id=goal_id)
        notice = _no_progress_notice(current)
        if notice is None:
            return False
        return queue_host_notice(store, thread_id, notice, replace_same_code=True)


# LLM: 只有处于熔断暂停状态的 Goal 才生成提示；notice_id 是去重与取走的唯一标识，
#   source/code 供同来源替换。文案由宿主固定生成，不读模型正文。
# 函数用途: 按当前 Goal 的 fuse 状态构造熔断提示，未暂停或原因码不符时返回 None。
def _no_progress_notice(goal: object) -> object | None:
    from .host_notices import host_notice

    thread_id = str(getattr(goal, "thread_id", "") or "")
    goal_id = str(getattr(goal, "goal_id", "") or "")
    metadata = getattr(goal, "metadata", {})
    current_state = metadata.get(FUSE_METADATA_KEY) if isinstance(metadata, Mapping) else {}
    if (
        goal is None
        or not thread_id
        or not goal_id
        or getattr(goal, "status", "") != "paused"
        or not isinstance(current_state, Mapping)
        or current_state.get("reason_code") != NO_PROGRESS_REASON_CODE
    ):
        return None
    current_count = int(current_state.get("idle_slices", 0) or 0)
    return host_notice(
        NO_PROGRESS_NOTICE_SOURCE,
        NO_PROGRESS_REASON_CODE,
        f"持续目标连续 {current_count} 片没有成功的工具调用或目标/任务状态变化，自动续跑已暂停。请检查进度后用 /goal resume 恢复。",
        details={"goal_id": goal_id, "reason_code": NO_PROGRESS_REASON_CODE},
    )


# LLM: 文案由宿主固定生成，只列结构化结束原因（turn_end_reason/runtime_reason/runtime_status），不读模型正文或显示快照，
#   因此不会带出凭据；三项都空（如冻结重投没有新执行）时写“结束原因不可取得”。details 由 host_notice 清洗并截长。
# 函数用途: 构造“上一片结束原因让自动续跑没接上”的宿主提示，并给出可执行的下一步。
def goal_withheld_notice(goal: object, signal: object, report: object) -> object:
    from .host_notices import host_notice

    facts = {
        "turn_end_reason": str(getattr(report, "turn_end_reason", "") or "").strip(),
        "runtime_reason": str(getattr(report, "runtime_reason", "") or "").strip(),
        "runtime_status": str(getattr(report, "runtime_status", "") or "").strip(),
    }
    named = "，".join(f"{key}={value}" for key, value in facts.items() if value) or "结束原因不可取得"
    return host_notice(
        NO_PROGRESS_NOTICE_SOURCE,
        WITHHELD_REASON_CODE,
        f"持续目标上一片结束后没有自动续跑（{named}）。目标仍在进行；用 /goal resume 接着跑，也可以直接回复，"
        "或用 /goal pause 暂停。",
        details={
            "goal_id": str(getattr(goal, "goal_id", "") or ""),
            "goal_revision": int(getattr(goal, "revision", 0) or 0),
            "wake_signal_id": str(getattr(signal, "wake_signal_id", "") or ""),
            **facts,
        },
    )


# LLM: 熔断提示的“主动推送”入口：沿共享推送规则排队并对可外发通道主动推一次（见 _push_goal_notice）。
# 函数用途: 熔断时对外发通道线程主动推一次提示，并保证同一提示不会重复送达。
def push_goal_no_progress_notice(
    store: object,
    goal: object,
    *,
    channels: object,
    route_deps: object,
) -> bool:
    notice = _no_progress_notice(goal)
    if notice is None:
        return False
    goal_id = str(getattr(goal, "goal_id", "") or "")
    return _push_goal_notice(store, goal, notice, _GoalNoticePush(
        channels, route_deps, f"goal-no-progress:{goal_id}", "thread_goal_no_progress_push",
    ))


# LLM: 续跑被扣下时的推送入口；每个线程同代码只留一条待送达，幂等键按目标与唤醒区分。
# 函数用途: 把“续跑没接上”的提示排队，并对外发通道线程主动推一次。
def push_goal_withheld_notice(
    store: object,
    goal: object,
    signal: object,
    report: object,
    *,
    channels: object,
    route_deps: object,
) -> bool:
    goal_id = str(getattr(goal, "goal_id", "") or "")
    wake_id = str(getattr(signal, "wake_signal_id", "") or "")
    return _push_goal_notice(store, goal, goal_withheld_notice(goal, signal, report), _GoalNoticePush(
        channels, route_deps, f"goal-withheld:{goal_id}:{wake_id}", "thread_goal_withheld_push",
    ))


# LLM: 一次主动推送需要的投递面、路由依赖、幂等键与会话记录原因；只为保持共享推送函数参数简短。
# 类用途: 打包目标提示主动推送的投递参数。
@dataclass(frozen=True)
class _GoalNoticePush:
    channels: object
    route_deps: object
    idempotency_key: str
    record_reason: str


# LLM: 目标提示的“主动推送”共享实现：先排队（同来源同代码替换），再对可主动外呼的通道立即投递一次并把同一条 notice
#   取走（已读，下一条回复不再重复带出）；纯 TUI/无外发通道线程只排队。投递失败不取走，提示留在待送达队列由下一次前台
#   回复补出。副作用：改写 pending_host_notices、可能调用外部投递服务并在成功时追加一条会话消息。
# 函数用途: 排队一条目标提示，并对外发通道线程主动推一次、保证同一提示不会重复送达。
def _push_goal_notice(store: object, goal: object, notice: object, push: _GoalNoticePush) -> bool:
    from .background_routing import resolve_background_route
    from .channels import DeliveryContext, ReplyEnvelope
    from .host_notices import queue_host_notice, take_host_notices

    thread_id = str(getattr(goal, "thread_id", "") or "")
    goal_id = str(getattr(goal, "goal_id", "") or "")
    if not thread_id or not goal_id:
        return False
    if not queue_host_notice(store, thread_id, notice, replace_same_code=True):
        return False
    channel, target = resolve_background_route(push.route_deps, thread_id)
    try:
        supports_proactive = bool(target and push.channels.supports_proactive(channel))
    except Exception:  # noqa: BLE001 - 主动推送失败只保留待送达提示，不影响熔断主链。
        supports_proactive = False
    if not supports_proactive:
        return True
    receipt = push.channels.deliver(
        DeliveryContext(
            channel=channel,
            target=target,
            mode="proactive",
            thread_id=thread_id,
            task_id=str(getattr(goal, "task_id", "") or ""),
            idempotency_key=push.idempotency_key,
        ),
        ReplyEnvelope(content=notice.text),
    )
    if str(getattr(receipt, "delivery_status", "") or "").strip().lower() == "sent":
        taken = take_host_notices(store, thread_id, [notice.notice_id])
        if taken:
            _record_pushed_notice_message(store, goal, notice, (channel, push.record_reason))
    return True


# LLM: 与额度通知同一模式：主动推送的提示也留一条会话记录，让同一线程的 TUI 用户能回看已送达内容。
#   delivered 是 (通道, 记录原因)；source/code 取自提示本身。
# 函数用途: 把已主动推送的目标提示追加为一条后台 assistant 消息。
def _record_pushed_notice_message(store: object, goal: object, notice: object, delivered: tuple[str, str]) -> None:
    channel, reason = delivered
    thread_id = str(getattr(goal, "thread_id", "") or "")
    if not thread_id or store is None or getattr(store, "messages", None) is None:
        return
    try:
        store.messages.append(
            {
                "thread_id": thread_id,
                "role": "assistant",
                "content": str(getattr(notice, "text", "") or ""),
                "channel": str(channel or "internal"),
                "metadata": {
                    "reason": reason,
                    "notice_id": str(getattr(notice, "notice_id", "") or ""),
                    "source": str(getattr(notice, "source", "") or ""),
                    "code": str(getattr(notice, "code", "") or ""),
                    "goal_id": str(getattr(goal, "goal_id", "") or ""),
                    "task_id": str(getattr(goal, "task_id", "") or ""),
                },
            }
        )
    except Exception:  # noqa: BLE001 - 落记录失败不反噬已成功的主动推送。
        _LOGGER.warning("goal notice message record failed(thread=%s)", thread_id, exc_info=True)


__all__ = [
    "FUSE_METADATA_KEY",
    "NO_PROGRESS_NOTICE_SOURCE",
    "NO_PROGRESS_REASON_CODE",
    "WAKE_SNAPSHOT_KEY",
    "WITHHELD_REASON_CODE",
    "goal_progress_snapshot",
    "goal_withheld_notice",
    "next_idle_slice_count",
    "push_goal_no_progress_notice",
    "push_goal_withheld_notice",
    "queue_goal_no_progress_notice",
    "record_goal_continuation_fuse",
    "reset_goal_continuation_fuse",
    "reset_goal_progress_fuse",
    "slice_has_progress",
]
